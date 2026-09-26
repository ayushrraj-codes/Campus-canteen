from datetime import datetime, timezone
import base64
import hashlib
import hmac
from io import BytesIO
import json
import re
import secrets
from urllib.parse import urlencode

import qrcode
import requests
from google.auth.exceptions import GoogleAuthError
from google.auth.transport.requests import Request as GoogleRequest
from google.oauth2 import id_token as google_id_token
from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request, status
from sqlalchemy import func, inspect, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, joinedload

from app.ai.wait_time import predict_wait_time
from app.config import get_settings
from app.database import get_db
from app.menu_data import MENU_ITEMS, RETIRED_MENU_ITEM_IDS
from app.models import MenuItem, Order, OrderItem, Payment, User
from app.schemas import AvailabilityUpdate, CreateOrderRequest, GoogleAuthRequest, LoginRequest, ProfileUpdate, RegisterRequest, StatusUpdate, UpiReferenceRequest, VerifyPaymentRequest
from app.security import create_access_token, get_current_user, hash_password, require_admin, require_customer, verify_password

router = APIRouter(prefix="/api")


def epoch_milliseconds(value: datetime) -> int:
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return int(value.timestamp() * 1000)


def user_json(user: User) -> dict:
    return {"id": user.id, "email": user.email, "name": user.name, "role": user.role, "mobile": user.mobile, "photoUrl": user.photo_url}


def menu_json(item: MenuItem) -> dict:
    return {"id": item.id, "name": item.name, "category": item.category, "cuisine": item.cuisine, "price": item.price, "description": item.description, "tag": item.tag, "meta": item.meta, "image": item.image, "alt": item.alt, "is_combo": item.is_combo, "available": item.available}


def order_json(order: Order) -> dict:
    result = {
        "id": f"#{order.token}", "token": order.token, "customerName": order.customer.name,
        "customerEmail": order.customer.email, "items": [{"id": item.menu_item_id, "name": item.item_name, "quantity": item.quantity, "price": item.unit_price} for item in order.items],
        "total": order.total, "pickupTime": order.pickup_time, "status": order.status,
        "createdAt": epoch_milliseconds(order.created_at), "prepMinutes": order.prep_minutes,
    }
    if order.payment:
        result["payment"] = {"transactionId": order.payment.transaction_id if order.payment.status in {"Paid", "Submitted"} else None, "providerOrderId": order.payment.provider_order_id, "amount": order.payment.amount, "method": order.payment.method, "status": order.payment.status, "paidAt": epoch_milliseconds(order.payment.paid_at) if order.payment.status == "Paid" else None}
    return result


def razorpay_checkout_data(order: Order, user: User) -> dict:
    settings = get_settings()
    if not settings.razorpay_key_id or not settings.razorpay_key_secret:
        raise HTTPException(status_code=503, detail="UPI checkout is not configured. Add Razorpay test or live keys to the backend environment.")
    if not order.payment or not order.payment.provider_order_id:
        raise HTTPException(status_code=409, detail="This order has no pending UPI payment.")
    return {
        "key_id": settings.razorpay_key_id,
        "order_id": order.payment.provider_order_id,
        "amount": order.total * 100,
        "currency": "INR",
        "name": "GEC Khagaria Canteen",
        "description": f"Canteen order #{order.token}",
        "prefill": {"name": user.name, "email": user.email, "contact": user.mobile or ""},
    }


def direct_upi_checkout_data(order: Order) -> dict:
    settings = get_settings()
    if not re.fullmatch(r"[A-Za-z0-9._-]{2,256}@[A-Za-z0-9.-]{2,64}", settings.upi_vpa):
        raise HTTPException(status_code=503, detail="The canteen UPI ID is not configured correctly.")
    reference = f"GEC{order.token}{datetime.now(timezone.utc):%y%m%d}"
    uri = "upi://pay?" + urlencode({
        "pa": settings.upi_vpa,
        "pn": settings.upi_payee_name,
        "am": f"{order.total:.2f}",
        "cu": "INR",
        "tn": f"GEC Canteen order {order.token}",
        "tr": reference,
    })
    qr = qrcode.QRCode(version=None, error_correction=qrcode.constants.ERROR_CORRECT_M, box_size=7, border=3)
    qr.add_data(uri)
    qr.make(fit=True)
    image = qr.make_image(fill_color="#202b22", back_color="white")
    buffer = BytesIO()
    image.save(buffer, format="PNG")
    encoded = base64.b64encode(buffer.getvalue()).decode("ascii")
    return {"upi_uri": uri, "qr_image": f"data:image/png;base64,{encoded}", "vpa": settings.upi_vpa, "payee_name": settings.upi_payee_name, "amount": order.total, "currency": "INR", "reference": reference}


def create_razorpay_order(order: Order, user: User) -> str:
    settings = get_settings()
    if not settings.razorpay_key_id or not settings.razorpay_key_secret:
        raise HTTPException(status_code=503, detail="UPI checkout is not configured. Add Razorpay test or live keys to the backend environment.")
    try:
        response = requests.post(
            "https://api.razorpay.com/v1/orders",
            auth=(settings.razorpay_key_id, settings.razorpay_key_secret),
            json={"amount": order.total * 100, "currency": "INR", "receipt": f"canteen-{order.token}", "notes": {"canteen_token": str(order.token), "customer_email": user.email}},
            timeout=12,
        )
        response.raise_for_status()
        result = response.json()
    except (requests.RequestException, ValueError):
        raise HTTPException(status_code=502, detail="Could not start UPI checkout with the payment provider. No order was placed.") from None
    if not isinstance(result, dict) or result.get("currency") != "INR" or result.get("amount") != order.total * 100 or not str(result.get("id", "")).startswith("order_"):
        raise HTTPException(status_code=502, detail="The payment provider returned an invalid checkout order.")
    return result["id"]


def mark_upi_payment_paid(payment: Payment, provider_payment: dict) -> None:
    now = datetime.now(timezone.utc)
    provider_created = provider_payment.get("created_at")
    if provider_created:
        now = datetime.fromtimestamp(int(provider_created), timezone.utc)
    payment.provider_payment_id = provider_payment["id"]
    payment.transaction_id = provider_payment["id"]
    payment.method = "UPI"
    payment.status = "Paid"
    payment.paid_at = now
    payment.order.status = "New"


def verify_provider_payment(order_id: str, payment_id: str, signature: str, expected_amount_rupees: int) -> dict:
    settings = get_settings()
    if not settings.razorpay_key_id or not settings.razorpay_key_secret:
        raise HTTPException(status_code=503, detail="UPI checkout is not configured on this server.")
    expected_signature = hmac.new(settings.razorpay_key_secret.encode(), f"{order_id}|{payment_id}".encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected_signature, signature):
        raise HTTPException(status_code=400, detail="Payment verification failed. The signature is invalid.")
    try:
        response = requests.get(
            f"https://api.razorpay.com/v1/payments/{payment_id}",
            auth=(settings.razorpay_key_id, settings.razorpay_key_secret),
            timeout=12,
        )
        response.raise_for_status()
        payment = response.json()
        if payment.get("order_id") != order_id or payment.get("amount") != expected_amount_rupees * 100 or payment.get("currency") != "INR" or payment.get("method") != "upi" or not payment.get("id"):
            raise HTTPException(status_code=409, detail="The verified payment does not match this order amount.")
        if payment.get("status") == "authorized":
            capture_response = requests.post(
                f"https://api.razorpay.com/v1/payments/{payment_id}/capture",
                auth=(settings.razorpay_key_id, settings.razorpay_key_secret),
                json={"amount": expected_amount_rupees * 100, "currency": "INR"},
                timeout=12,
            )
            capture_response.raise_for_status()
            payment = capture_response.json()
        if payment.get("status") != "captured":
            raise HTTPException(status_code=409, detail="The UPI payment has not been captured. The order is not sent to the kitchen.")
        return payment
    except requests.RequestException:
        raise HTTPException(status_code=502, detail="Could not confirm payment status with the provider. Your order remains pending; retry verification shortly.") from None
    except ValueError:
        raise HTTPException(status_code=502, detail="The payment provider returned an unreadable verification response.") from None


@router.get("/health")
def health():
    return {"status": "ok", "service": "gec-khagaria-canteen-api"}


@router.get("/menu")
def list_menu(db: Session = Depends(get_db)):
    items = db.scalars(select(MenuItem).where(MenuItem.active.is_(True)).order_by(MenuItem.is_combo.desc(), MenuItem.category, MenuItem.name)).all()
    return {"items": [menu_json(item) for item in items]}


@router.post("/auth/register", status_code=status.HTTP_201_CREATED)
def register(payload: RegisterRequest, db: Session = Depends(get_db)):
    if db.scalar(select(User.id).where(User.email == payload.email)):
        raise HTTPException(status_code=409, detail="An account with this email already exists. Try logging in instead.")
    user = User(email=payload.email, name=payload.name, role=payload.role, mobile=payload.mobile, password_hash=hash_password(payload.password))
    db.add(user)
    db.commit()
    db.refresh(user)
    return {"access_token": create_access_token(user), "token_type": "bearer", "user": user_json(user)}


@router.post("/auth/login")
def login(payload: LoginRequest, db: Session = Depends(get_db)):
    user = db.scalar(select(User).where(User.email == payload.email))
    if user is None or not verify_password(payload.password, user.password_hash):
        raise HTTPException(status_code=401, detail="Email or password is incorrect.")
    return {"access_token": create_access_token(user), "token_type": "bearer", "user": user_json(user)}


@router.post("/auth/google")
def google_login(payload: GoogleAuthRequest, db: Session = Depends(get_db)):
    settings = get_settings()
    if not settings.google_client_id:
        raise HTTPException(status_code=503, detail="Google sign-in is not configured. Set GOOGLE_CLIENT_ID on the backend and frontend.")
    try:
        claims = google_id_token.verify_oauth2_token(payload.credential, GoogleRequest(), settings.google_client_id)
    except (ValueError, GoogleAuthError):
        raise HTTPException(status_code=401, detail="Google could not verify this sign-in. Try again.") from None
    email = str(claims.get("email", "")).strip().lower()
    if not claims.get("email_verified") or not email or not claims.get("sub"):
        raise HTTPException(status_code=401, detail="Use a Google account with a verified email address.")
    user = db.scalar(select(User).where(User.email == email))
    if user is not None and user.role == "admin":
        raise HTTPException(status_code=403, detail="Kitchen staff must sign in with their staff credentials.")
    if user is None:
        user = User(
            email=email,
            name=str(claims.get("name") or email.split("@", 1)[0])[:70],
            role=payload.role,
            photo_url=claims.get("picture") if str(claims.get("picture", "")).startswith("https://") else None,
            password_hash=hash_password(secrets.token_urlsafe(48)),
        )
        db.add(user)
        db.commit()
        db.refresh(user)
    elif not user.photo_url and str(claims.get("picture", "")).startswith("https://"):
        user.photo_url = claims["picture"]
        db.commit()
    return {"access_token": create_access_token(user), "token_type": "bearer", "user": user_json(user)}


@router.get("/auth/me")
def me(user: User = Depends(get_current_user)):
    return user_json(user)


@router.patch("/profile/me")
def update_profile(payload: ProfileUpdate, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    changes = payload.model_dump(exclude_unset=True)
    if not changes:
        raise HTTPException(status_code=422, detail="Provide at least one profile field to update.")
    if "name" in changes and changes["name"] is None:
        raise HTTPException(status_code=422, detail="Name cannot be empty.")
    for field, value in changes.items():
        setattr(user, field, value)
    db.commit()
    db.refresh(user)
    return user_json(user)


@router.get("/predict/wait-time")
def wait_time(db: Session = Depends(get_db)):
    orders = db.scalars(select(Order)).all()
    active = sum(order.status in {"New", "Cooking", "Ready"} for order in orders)
    cooking = sum(order.status == "Cooking" for order in orders)
    samples = [order.prep_minutes for order in db.scalars(select(Order).where(Order.prep_minutes.is_not(None)).order_by(Order.ready_at.desc()).limit(30)).all() if order.prep_minutes]
    return predict_wait_time(active, cooking, list(reversed(samples)))


@router.post("/orders", status_code=status.HTTP_201_CREATED)
def create_order(payload: CreateOrderRequest, user: User = Depends(require_customer), db: Session = Depends(get_db)):
    quantities: dict[str, int] = {}
    for line in payload.items:
        quantities[line.item_id] = quantities.get(line.item_id, 0) + line.quantity
    if any(quantity > 20 for quantity in quantities.values()):
        raise HTTPException(status_code=422, detail="You can order up to 20 of each menu item.")
    menu_items = {item.id: item for item in db.scalars(select(MenuItem).where(MenuItem.id.in_(quantities), MenuItem.active.is_(True))).all()}
    if len(menu_items) != len(quantities):
        raise HTTPException(status_code=422, detail="One or more menu items are no longer available.")
    unavailable = [item.name for item in menu_items.values() if not item.available]
    if unavailable:
        raise HTTPException(status_code=409, detail=f"Currently out of stock: {', '.join(unavailable)}.")
    total = sum(menu_items[item_id].price * quantity for item_id, quantity in quantities.items())
    token = (db.scalar(select(func.max(Order.token))) or 203) + 1
    order = Order(token=token, customer_id=user.id, pickup_time=payload.pickup_time, total=total, status="AwaitingPayment" if payload.payment_method == "upi" else "New")
    order.items = [OrderItem(menu_item_id=item_id, item_name=menu_items[item_id].name, unit_price=menu_items[item_id].price, quantity=quantity) for item_id, quantity in quantities.items()]
    db.add(order)
    manual_upi = False
    upi_checkout = None
    if payload.payment_method == "upi":
        db.flush()
        settings = get_settings()
        if settings.razorpay_key_id and settings.razorpay_key_secret:
            try:
                provider_order_id = create_razorpay_order(order, user)
            except HTTPException:
                db.rollback()
                raise
            order.payment = Payment(order_id=order.id, transaction_id=f"PENDING-{order.token}", amount=total, method="UPI", status="Pending", provider_order_id=provider_order_id)
        else:
            try:
                upi_checkout = direct_upi_checkout_data(order)
            except HTTPException:
                db.rollback()
                raise
            manual_upi = True
            order.payment = Payment(order_id=order.id, transaction_id=f"UPI-PENDING-{order.token}", amount=total, method="UPI manual", status="Pending")
    db.commit()
    order = db.scalar(select(Order).options(joinedload(Order.customer), joinedload(Order.items), joinedload(Order.payment)).where(Order.id == order.id))
    result = order_json(order)
    if payload.payment_method == "upi":
        if manual_upi:
            result["upi"] = upi_checkout
        else:
            result["razorpay"] = razorpay_checkout_data(order, user)
    return result


@router.get("/orders/me")
def my_orders(user: User = Depends(require_customer), db: Session = Depends(get_db)):
    orders = db.scalars(select(Order).options(joinedload(Order.customer), joinedload(Order.items), joinedload(Order.payment)).where(Order.customer_id == user.id).order_by(Order.created_at.desc())).unique().all()
    return {"orders": [order_json(order) for order in orders]}


@router.get("/orders")
def all_orders(order_filter: str = Query(default="Active", pattern="^(Active|All|Ready|Collected|Payment review)$"), _admin: User = Depends(require_admin), db: Session = Depends(get_db)):
    statement = select(Order).options(joinedload(Order.customer), joinedload(Order.items), joinedload(Order.payment)).order_by(Order.created_at.desc())
    if order_filter == "Active":
        statement = statement.where(Order.status.in_(["New", "Cooking", "Ready"]))
    elif order_filter != "All":
        if order_filter == "Payment review":
            statement = statement.join(Payment).where(Order.status == "AwaitingPayment", Payment.status == "Submitted")
        else:
            statement = statement.where(Order.status == order_filter)
    orders = db.scalars(statement).unique().all()
    counts = db.execute(select(Order.status, func.count(Order.id)).group_by(Order.status)).all()
    return {"orders": [order_json(order) for order in orders], "summary": {"active": sum(count for state, count in counts if state in {"New", "Cooking", "Ready"}), "ready": sum(count for state, count in counts if state == "Ready"), "collected": sum(count for state, count in counts if state == "Collected")}}


@router.patch("/orders/{token}/status")
def update_order_status(token: int, payload: StatusUpdate, _admin: User = Depends(require_admin), db: Session = Depends(get_db)):
    order = db.scalar(select(Order).options(joinedload(Order.customer), joinedload(Order.items), joinedload(Order.payment)).where(Order.token == token))
    if order is None:
        raise HTTPException(status_code=404, detail="Order was not found.")
    transitions = {"New": "Cooking", "Cooking": "Ready", "Ready": "Collected"}
    if transitions.get(order.status) != payload.status:
        raise HTTPException(status_code=409, detail=f"Order cannot move from {order.status} to {payload.status}.")
    now = datetime.now(timezone.utc)
    order.status = payload.status
    if payload.status == "Cooking":
        order.cooking_at = now
    elif payload.status == "Ready":
        order.ready_at = now
        started = order.cooking_at or order.created_at
        if started.tzinfo is None:
            started = started.replace(tzinfo=timezone.utc)
        order.prep_minutes = max(1, round((now - started).total_seconds() / 60))
    else:
        order.collected_at = now
        if not order.payment:
            order.payment = Payment(order_id=order.id, transaction_id=f"GEC-{now:%Y%m%d}-{order.token:04d}", amount=order.total, paid_at=now)
    db.commit()
    db.refresh(order)
    return order_json(order)


@router.post("/payments/orders/{token}/reference")
def submit_upi_reference(token: int, payload: UpiReferenceRequest, user: User = Depends(require_customer), db: Session = Depends(get_db)):
    order = db.scalar(select(Order).options(joinedload(Order.customer), joinedload(Order.items), joinedload(Order.payment)).where(Order.token == token, Order.customer_id == user.id))
    if order is None:
        raise HTTPException(status_code=404, detail="Order was not found.")
    if order.status != "AwaitingPayment" or not order.payment or order.payment.method != "UPI manual" or order.payment.status not in {"Pending", "Submitted"}:
        raise HTTPException(status_code=409, detail="This order is not awaiting a direct UPI reference.")
    order.payment.transaction_id = payload.reference
    order.payment.status = "Submitted"
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(status_code=409, detail="That UPI reference was already submitted.") from None
    return order_json(order)


@router.post("/admin/orders/{token}/confirm-upi")
def confirm_manual_upi_payment(token: int, _admin: User = Depends(require_admin), db: Session = Depends(get_db)):
    order = db.scalar(select(Order).options(joinedload(Order.customer), joinedload(Order.items), joinedload(Order.payment)).where(Order.token == token))
    if order is None:
        raise HTTPException(status_code=404, detail="Order was not found.")
    if order.status != "AwaitingPayment" or not order.payment or order.payment.method != "UPI manual" or order.payment.status != "Submitted":
        raise HTTPException(status_code=409, detail="Order has no submitted direct UPI reference to verify.")
    order.payment.method = "UPI"
    order.payment.status = "Paid"
    order.payment.paid_at = datetime.now(timezone.utc)
    order.status = "New"
    db.commit()
    return order_json(order)


@router.post("/payments/verify")
def verify_upi_payment(payload: VerifyPaymentRequest, user: User = Depends(require_customer), db: Session = Depends(get_db)):
    payment = db.scalar(select(Payment).options(joinedload(Payment.order).joinedload(Order.customer), joinedload(Payment.order).joinedload(Order.items)).where(Payment.provider_order_id == payload.razorpay_order_id))
    if payment is None or payment.order.customer_id != user.id:
        raise HTTPException(status_code=404, detail="Pending UPI order was not found for this account.")
    if payment.status == "Paid":
        if payment.provider_payment_id == payload.razorpay_payment_id:
            return order_json(payment.order)
        raise HTTPException(status_code=409, detail="This order already has a different verified payment.")
    provider_payment = verify_provider_payment(payload.razorpay_order_id, payload.razorpay_payment_id, payload.razorpay_signature, payment.amount)
    mark_upi_payment_paid(payment, provider_payment)
    db.commit()
    db.refresh(payment.order)
    return order_json(payment.order)


@router.post("/payments/orders/{token}/checkout")
def retry_upi_checkout(token: int, user: User = Depends(require_customer), db: Session = Depends(get_db)):
    order = db.scalar(select(Order).options(joinedload(Order.customer), joinedload(Order.items), joinedload(Order.payment)).where(Order.token == token, Order.customer_id == user.id))
    if order is None:
        raise HTTPException(status_code=404, detail="Order was not found.")
    if order.status != "AwaitingPayment" or not order.payment or order.payment.status not in {"Pending", "Submitted"}:
        raise HTTPException(status_code=409, detail="This order has no pending UPI checkout.")
    if order.payment.method == "UPI manual":
        return {"order": order_json(order), "upi": direct_upi_checkout_data(order)}
    return {"order": order_json(order), "razorpay": razorpay_checkout_data(order, user)}


@router.post("/payments/webhook")
async def razorpay_webhook(request: Request, x_razorpay_signature: str | None = Header(default=None), db: Session = Depends(get_db)):
    webhook_secret = get_settings().razorpay_webhook_secret
    if not webhook_secret:
        raise HTTPException(status_code=503, detail="Razorpay webhooks are not configured.")
    raw_body = await request.body()
    expected_signature = hmac.new(webhook_secret.encode(), raw_body, hashlib.sha256).hexdigest()
    if not x_razorpay_signature or not hmac.compare_digest(expected_signature, x_razorpay_signature):
        raise HTTPException(status_code=400, detail="Webhook signature is invalid.")
    try:
        event = json.loads(raw_body)
    except (json.JSONDecodeError, UnicodeDecodeError):
        raise HTTPException(status_code=400, detail="Webhook payload is invalid JSON.") from None
    if event.get("event") != "payment.captured":
        return {"received": True, "processed": False}
    entity = event.get("payload", {}).get("payment", {}).get("entity", {})
    if not isinstance(entity, dict):
        raise HTTPException(status_code=400, detail="Webhook payment details are invalid.")
    payment = db.scalar(select(Payment).options(joinedload(Payment.order).joinedload(Order.customer), joinedload(Payment.order).joinedload(Order.items)).where(Payment.provider_order_id == entity.get("order_id")))
    if payment is None:
        return {"received": True, "processed": False}
    if entity.get("amount") != payment.amount * 100 or entity.get("currency") != "INR" or entity.get("status") != "captured" or entity.get("method") != "upi" or not entity.get("id"):
        raise HTTPException(status_code=409, detail="Captured payment does not match the canteen order.")
    if payment.status != "Paid":
        mark_upi_payment_paid(payment, entity)
        db.commit()
    return {"received": True, "processed": True}


@router.patch("/admin/menu/{item_id}/availability")
def set_availability(item_id: str, payload: AvailabilityUpdate, _admin: User = Depends(require_admin), db: Session = Depends(get_db)):
    item = db.get(MenuItem, item_id)
    if item is None or not item.active:
        raise HTTPException(status_code=404, detail="Menu item was not found.")
    item.available = payload.available
    db.commit()
    return menu_json(item)


@router.get("/payments/me")
def my_payments(user: User = Depends(require_customer), db: Session = Depends(get_db)):
    payments = db.scalars(select(Payment).join(Order).where(Order.customer_id == user.id, Payment.status == "Paid").order_by(Payment.paid_at.desc())).all()
    return {"payments": [{"transactionId": payment.transaction_id, "orderId": f"#{payment.order.token}", "amount": payment.amount, "method": payment.method, "status": payment.status, "paidAt": epoch_milliseconds(payment.paid_at)} for payment in payments]}


def initialize_database():
    from app.database import Base, engine
    from app import models  # noqa: F401

    Base.metadata.create_all(bind=engine)
    menu_columns = {column["name"] for column in inspect(engine).get_columns("menu_items")}
    with engine.begin() as connection:
        if "cuisine" not in menu_columns:
            connection.execute(text("ALTER TABLE menu_items ADD COLUMN cuisine VARCHAR(24)"))
        if "active" not in menu_columns:
            connection.execute(text("ALTER TABLE menu_items ADD COLUMN active BOOLEAN NOT NULL DEFAULT TRUE"))
    user_columns = {column["name"] for column in inspect(engine).get_columns("users")}
    payment_columns = {column["name"] for column in inspect(engine).get_columns("payments")}
    with engine.begin() as connection:
        if "mobile" not in user_columns:
            connection.execute(text("ALTER TABLE users ADD COLUMN mobile VARCHAR(16)"))
        if "photo_url" not in user_columns:
            connection.execute(text("ALTER TABLE users ADD COLUMN photo_url TEXT"))
        if "provider_order_id" not in payment_columns:
            connection.execute(text("ALTER TABLE payments ADD COLUMN provider_order_id VARCHAR(64)"))
        if "provider_payment_id" not in payment_columns:
            connection.execute(text("ALTER TABLE payments ADD COLUMN provider_payment_id VARCHAR(64)"))
        connection.execute(text("CREATE UNIQUE INDEX IF NOT EXISTS uq_payments_provider_order_id ON payments(provider_order_id)"))
        connection.execute(text("CREATE UNIQUE INDEX IF NOT EXISTS uq_payments_provider_payment_id ON payments(provider_payment_id)"))
    with Session(engine) as db:
        for item_data in MENU_ITEMS:
            item_data = {**item_data, "available": item_data.get("available", True), "active": True}
            item = db.get(MenuItem, item_data["id"])
            if item is None:
                db.add(MenuItem(**item_data))
            else:
                for key in ("cuisine", "is_combo", "active"):
                    if key in item_data:
                        setattr(item, key, item_data[key])
        for item_id in RETIRED_MENU_ITEM_IDS:
            item = db.get(MenuItem, item_id)
            if item is not None:
                item.active = False
                item.available = False
        settings = get_settings()
        if settings.admin_email:
            admin = db.scalar(select(User).where(User.email == settings.admin_email.lower()))
            if admin is None:
                db.add(User(email=settings.admin_email.lower(), name=settings.admin_name, role="admin", password_hash=hash_password(settings.admin_password)))
            else:
                admin.name = settings.admin_name
                admin.role = "admin"
                if not verify_password(settings.admin_password, admin.password_hash):
                    admin.password_hash = hash_password(settings.admin_password)
        db.commit()