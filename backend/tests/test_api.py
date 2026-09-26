import hashlib
import hmac
import json
from urllib.parse import parse_qs, urlsplit

from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.database import Base, get_db
import app.api as api_module
from app.api import google_id_token
from app.config import get_settings
from app.main import app
from app.models import User
from app.security import create_access_token, hash_password

test_engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
TestSession = sessionmaker(bind=test_engine, autoflush=False, expire_on_commit=False)


def override_get_db():
    with TestSession() as session:
        yield session


app.dependency_overrides[get_db] = override_get_db
Base.metadata.create_all(bind=test_engine)
with TestSession() as session:
    from app.menu_data import MENU_ITEMS
    from app.models import MenuItem
    for item_data in MENU_ITEMS:
        session.add(MenuItem(**{**item_data, "available": item_data.get("available", True)}))
    session.add(MenuItem(id="cheese-garlic-bread", name="Cheese Garlic Bread", category="Snacks", cuisine="Italian", price=75, description="Retired menu item", tag="RETIRED", meta="", image="retired", alt="Retired item", active=False, available=False))
    session.commit()

client = TestClient(app)


class FakeGatewayResponse:
    def __init__(self, payload):
        self.payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self.payload


def test_menu_is_available_without_authentication():
    response = client.get("/api/menu")

    assert response.status_code == 200
    items = response.json()["items"]
    assert len(items) == 39
    assert next(item for item in items if item["id"] == "cold-drink")["available"] is False
    snacks = [item for item in items if item["category"] == "Snacks" and item.get("cuisine")]
    assert {item["cuisine"] for item in snacks} == {"Indian", "Chinese", "Korean", "Thai", "South Indian"}
    item_by_id = {item["id"]: item for item in items}
    assert item_by_id["iced-matcha"]["category"] == "Drinks"
    assert {item_by_id[item_id]["category"] for item_id in ("mint-mojito", "cold-coffee", "cafe-latte", "chocolate-shake", "virgin-fruit-cocktail")} == {"Drinks"}
    assert {item_by_id[item_id]["category"] for item_id in ("tomato-soup", "sweet-corn-soup")} == {"Meals"}
    assert "cheese-garlic-bread" not in item_by_id
    added_item_ids = {"veg-chow-mein", "veg-hakka-noodles", "veg-manchurian", "paneer-chilli", "honey-chilli-potato", "baby-corn-masala", "masala-papad", "chicken-pakora", "mushroom-chilli", "veg-momos", "chicken-roll"}
    assert added_item_ids.issubset(item_by_id)


def test_register_login_and_server_priced_order():
    signup = client.post("/api/auth/register", json={"name": "Test Student", "email": "student@example.edu", "password": "test-password-123", "role": "student"})
    assert signup.status_code == 201
    token = signup.json()["access_token"]
    headers = {"Authorization": f"Bearer {token}"}

    created = client.post("/api/orders", headers=headers, json={"items": [{"item_id": "banana-bread", "quantity": 2}], "pickup_time": "12:00 PM"})
    assert created.status_code == 201
    assert created.json()["total"] == 70
    assert created.json()["items"][0]["name"] == "Banana bread"

    login = client.post("/api/auth/login", json={"email": "STUDENT@example.edu", "password": "test-password-123"})
    assert login.status_code == 200


def test_order_rejects_unavailable_items():
    signup = client.post("/api/auth/register", json={"name": "Stock Check", "email": "stock@example.edu", "password": "test-password-123", "role": "student"})
    headers = {"Authorization": f"Bearer {signup.json()['access_token']}"}

    response = client.post("/api/orders", headers=headers, json={"items": [{"item_id": "cold-drink", "quantity": 1}], "pickup_time": "12:00 PM"})

    assert response.status_code == 409
    assert "out of stock" in response.json()["detail"]


def test_new_chow_mein_item_can_be_ordered_at_server_price():
    signup = client.post("/api/auth/register", json={"name": "Noodle Student", "email": "noodles@example.edu", "password": "test-password-123"})
    headers = {"Authorization": f"Bearer {signup.json()['access_token']}"}

    response = client.post("/api/orders", headers=headers, json={"items": [{"item_id": "veg-chow-mein", "quantity": 2}], "pickup_time": "12:15 PM"})

    assert response.status_code == 201
    assert response.json()["total"] == 170
    assert response.json()["items"][0]["name"] == "Vegetable Chow Mein"


def test_retired_menu_item_is_not_orderable():
    signup = client.post("/api/auth/register", json={"name": "Retired Item", "email": "retired@example.edu", "password": "test-password-123"})
    headers = {"Authorization": f"Bearer {signup.json()['access_token']}"}

    response = client.post("/api/orders", headers=headers, json={"items": [{"item_id": "cheese-garlic-bread", "quantity": 1}], "pickup_time": "12:00 PM"})

    assert response.status_code == 422
    assert "no longer available" in response.json()["detail"]


def test_upi_order_is_pending_until_server_verifies_captured_payment(monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "razorpay_key_id", "rzp_test_key")
    monkeypatch.setattr(settings, "razorpay_key_secret", "test-razorpay-secret")
    signup = client.post("/api/auth/register", json={"name": "UPI Student", "email": "upi@example.edu", "password": "test-password-123"})
    headers = {"Authorization": f"Bearer {signup.json()['access_token']}"}

    def create_provider_order(_url, auth, json, timeout):
        assert auth == ("rzp_test_key", "test-razorpay-secret")
        assert json["amount"] == 7000
        assert json["currency"] == "INR"
        return FakeGatewayResponse({"id": "order_test_12345", "amount": 7000, "currency": "INR"})

    monkeypatch.setattr(api_module.requests, "post", create_provider_order)
    created = client.post("/api/orders", headers=headers, json={"items": [{"item_id": "veg-burger", "quantity": 1}], "pickup_time": "12:00 PM", "payment_method": "upi"})

    assert created.status_code == 201
    pending_order = created.json()
    assert pending_order["status"] == "AwaitingPayment"
    assert pending_order["payment"]["status"] == "Pending"
    assert pending_order["razorpay"]["order_id"] == "order_test_12345"

    payment_payload = {"id": "pay_test_12345", "order_id": "order_test_12345", "amount": 7000, "currency": "INR", "method": "upi", "status": "captured", "created_at": 1780000000}
    monkeypatch.setattr(api_module.requests, "get", lambda *_args, **_kwargs: FakeGatewayResponse(payment_payload))
    signature = hmac.new(b"test-razorpay-secret", b"order_test_12345|pay_test_12345", hashlib.sha256).hexdigest()
    verified = client.post("/api/payments/verify", headers=headers, json={"razorpay_order_id": "order_test_12345", "razorpay_payment_id": "pay_test_12345", "razorpay_signature": signature})

    assert verified.status_code == 200
    assert verified.json()["status"] == "New"
    assert verified.json()["payment"]["status"] == "Paid"
    assert verified.json()["payment"]["transactionId"] == "pay_test_12345"


def test_direct_upi_fallback_requires_reference_then_staff_confirmation(monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "razorpay_key_id", None)
    monkeypatch.setattr(settings, "razorpay_key_secret", None)
    monkeypatch.setattr(settings, "upi_vpa", "adityaraj123beg@oksbi")
    active_before = client.get("/api/predict/wait-time").json()["active_orders"]
    signup = client.post("/api/auth/register", json={"name": "Direct UPI", "email": "direct-upi@example.edu", "password": "test-password-123"})
    customer_headers = {"Authorization": f"Bearer {signup.json()['access_token']}"}
    created = client.post("/api/orders", headers=customer_headers, json={"items": [{"item_id": "veg-chow-mein", "quantity": 1}], "pickup_time": "12:00 PM", "payment_method": "upi"})

    assert created.status_code == 201
    order = created.json()
    assert order["status"] == "AwaitingPayment"
    assert order["upi"]["qr_image"].startswith("data:image/png;base64,")
    uri = parse_qs(urlsplit(order["upi"]["upi_uri"]).query)
    assert uri["pa"] == ["adityaraj123beg@oksbi"]
    assert uri["am"] == ["85.00"]

    submitted = client.post(f"/api/payments/orders/{order['token']}/reference", headers=customer_headers, json={"reference": "123456789012"})
    assert submitted.status_code == 200
    assert submitted.json()["status"] == "AwaitingPayment"
    assert submitted.json()["payment"]["status"] == "Submitted"
    assert client.get("/api/predict/wait-time").json()["active_orders"] == active_before

    with TestSession() as session:
        admin = User(email="upi-review@example.edu", name="UPI Reviewer", role="admin", password_hash=hash_password("admin-password-123"))
        session.add(admin)
        session.commit()
        session.refresh(admin)
        admin_headers = {"Authorization": f"Bearer {create_access_token(admin)}"}
    review = client.get("/api/orders?order_filter=Payment%20review", headers=admin_headers)
    assert review.status_code == 200
    assert any(entry["token"] == order["token"] for entry in review.json()["orders"])

    confirmed = client.post(f"/api/admin/orders/{order['token']}/confirm-upi", headers=admin_headers)
    assert confirmed.status_code == 200
    assert confirmed.json()["status"] == "New"
    assert confirmed.json()["payment"]["status"] == "Paid"
    assert client.get("/api/predict/wait-time").json()["active_orders"] == active_before + 1


def test_direct_upi_reference_cannot_be_confirmed_by_customer(monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "razorpay_key_id", None)
    monkeypatch.setattr(settings, "razorpay_key_secret", None)
    signup = client.post("/api/auth/register", json={"name": "No Self Approval", "email": "no-self-approval@example.edu", "password": "test-password-123"})
    headers = {"Authorization": f"Bearer {signup.json()['access_token']}"}
    order = client.post("/api/orders", headers=headers, json={"items": [{"item_id": "samosa", "quantity": 1}], "pickup_time": "12:00 PM", "payment_method": "upi"}).json()
    submitted = client.post(f"/api/payments/orders/{order['token']}/reference", headers=headers, json={"reference": "123456789013"})
    assert submitted.status_code == 200

    response = client.post(f"/api/admin/orders/{order['token']}/confirm-upi", headers=headers)

    assert response.status_code == 403


def test_upi_order_uses_direct_vpa_when_gateway_keys_are_missing(monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "razorpay_key_id", None)
    monkeypatch.setattr(settings, "razorpay_key_secret", None)
    signup = client.post("/api/auth/register", json={"name": "No Gateway", "email": "no-gateway@example.edu", "password": "test-password-123"})
    headers = {"Authorization": f"Bearer {signup.json()['access_token']}"}

    response = client.post("/api/orders", headers=headers, json={"items": [{"item_id": "banana-bread", "quantity": 1}], "pickup_time": "12:00 PM", "payment_method": "upi"})

    assert response.status_code == 201
    assert response.json()["status"] == "AwaitingPayment"
    assert response.json()["upi"]["vpa"] == "adityaraj123beg@oksbi"
    assert "razorpay" not in response.json()
    assert client.get("/api/orders/me", headers=headers).json()["orders"][0]["status"] == "AwaitingPayment"


def test_razorpay_webhook_verifies_signature_and_marks_payment_paid(monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "razorpay_key_id", "rzp_test_webhook")
    monkeypatch.setattr(settings, "razorpay_key_secret", "test-webhook-key")
    monkeypatch.setattr(settings, "razorpay_webhook_secret", "test-webhook-secret")
    signup = client.post("/api/auth/register", json={"name": "Webhook Student", "email": "webhook@example.edu", "password": "test-password-123"})
    headers = {"Authorization": f"Bearer {signup.json()['access_token']}"}
    monkeypatch.setattr(api_module.requests, "post", lambda *_args, **_kwargs: FakeGatewayResponse({"id": "order_webhook_123", "amount": 8000, "currency": "INR"}))
    created = client.post("/api/orders", headers=headers, json={"items": [{"item_id": "soup-toastie", "quantity": 1}], "pickup_time": "12:00 PM", "payment_method": "upi"})
    assert created.status_code == 201

    event = {"event": "payment.captured", "payload": {"payment": {"entity": {"id": "pay_webhook_123", "order_id": "order_webhook_123", "amount": 8000, "currency": "INR", "method": "upi", "status": "captured"}}}}
    raw_body = json.dumps(event).encode()
    signature = hmac.new(b"test-webhook-secret", raw_body, hashlib.sha256).hexdigest()
    response = client.post("/api/payments/webhook", content=raw_body, headers={"Content-Type": "application/json", "X-Razorpay-Signature": signature})

    assert response.status_code == 200
    assert response.json() == {"received": True, "processed": True}
    order = client.get("/api/orders/me", headers=headers).json()["orders"][0]
    assert order["status"] == "New"
    assert order["payment"]["status"] == "Paid"


def test_kitchen_lifecycle_records_prep_time_and_payment():
    signup = client.post("/api/auth/register", json={"name": "Lifecycle Student", "email": "lifecycle@example.edu", "password": "test-password-123", "role": "student"})
    customer_headers = {"Authorization": f"Bearer {signup.json()['access_token']}"}
    order_response = client.post("/api/orders", headers=customer_headers, json={"items": [{"item_id": "veg-burger", "quantity": 1}], "pickup_time": "12:15 PM"})
    order = order_response.json()

    with TestSession() as session:
        admin = User(email="kitchen@example.edu", name="Kitchen Staff", role="admin", password_hash=hash_password("admin-password-123"))
        session.add(admin)
        session.commit()
        session.refresh(admin)
        admin_token = create_access_token(admin)
    admin_headers = {"Authorization": f"Bearer {admin_token}"}

    for state in ("Cooking", "Ready", "Collected"):
        response = client.patch(f"/api/orders/{order['token']}/status", headers=admin_headers, json={"status": state})
        assert response.status_code == 200
        order = response.json()

    assert order["status"] == "Collected"
    assert order["createdAt"] > 0
    assert order["prepMinutes"] >= 1
    assert order["payment"]["status"] == "Paid"


def test_user_can_update_name_mobile_and_validated_photo():
    signup = client.post("/api/auth/register", json={"name": "Profile Student", "email": "profile@example.edu", "password": "test-password-123", "mobile": "+91 98765 43210"})
    headers = {"Authorization": f"Bearer {signup.json()['access_token']}"}
    assert signup.json()["user"]["mobile"] == "+919876543210"

    photo_data = "data:image/png;base64,iVBORw0KGgo="
    updated = client.patch("/api/profile/me", headers=headers, json={"name": "Updated Student", "mobile": "9123456789", "photo_url": photo_data})
    assert updated.status_code == 200
    assert updated.json()["email"] == "profile@example.edu"
    assert updated.json()["name"] == "Updated Student"
    assert updated.json()["mobile"] == "9123456789"
    assert updated.json()["photoUrl"] == photo_data

    invalid_photo = client.patch("/api/profile/me", headers=headers, json={"photo_url": "data:image/svg+xml;base64,PHN2Zz4="})
    assert invalid_photo.status_code == 422


def test_google_login_creates_only_verified_customer(monkeypatch):
    monkeypatch.setattr(get_settings(), "google_client_id", "test-google-client-id")
    monkeypatch.setattr(google_id_token, "verify_oauth2_token", lambda *_args: {
        "email": "google@example.edu", "email_verified": True, "sub": "google-subject",
        "name": "Google User", "picture": "https://images.example.edu/avatar.jpg",
    })

    response = client.post("/api/auth/google", json={"credential": "test-google-id-token-credential", "role": "faculty"})

    assert response.status_code == 200
    assert response.json()["user"]["email"] == "google@example.edu"
    assert response.json()["user"]["role"] == "faculty"
    assert response.json()["user"]["photoUrl"] == "https://images.example.edu/avatar.jpg"


def test_google_login_rejects_unverified_email(monkeypatch):
    monkeypatch.setattr(get_settings(), "google_client_id", "test-google-client-id")
    monkeypatch.setattr(google_id_token, "verify_oauth2_token", lambda *_args: {
        "email": "unverified@example.edu", "email_verified": False, "sub": "unverified-subject",
    })

    response = client.post("/api/auth/google", json={"credential": "test-google-id-token-credential"})

    assert response.status_code == 401
