from datetime import datetime, timezone

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True)
    email: Mapped[str] = mapped_column(String(160), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(70))
    password_hash: Mapped[str] = mapped_column(String(256))
    role: Mapped[str] = mapped_column(String(16), default="student")
    mobile: Mapped[str | None] = mapped_column(String(16), nullable=True)
    photo_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    orders: Mapped[list["Order"]] = relationship(back_populates="customer")


class MenuItem(Base):
    __tablename__ = "menu_items"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    name: Mapped[str] = mapped_column(String(100))
    category: Mapped[str] = mapped_column(String(24), index=True)
    cuisine: Mapped[str | None] = mapped_column(String(24), nullable=True, index=True)
    price: Mapped[int] = mapped_column(Integer)
    description: Mapped[str] = mapped_column(String(240))
    tag: Mapped[str] = mapped_column(String(48))
    meta: Mapped[str] = mapped_column(String(48))
    image: Mapped[str] = mapped_column(String(80))
    alt: Mapped[str] = mapped_column(String(160))
    active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    is_combo: Mapped[bool] = mapped_column(Boolean, default=False)
    available: Mapped[bool] = mapped_column(Boolean, default=True)


class Order(Base):
    __tablename__ = "orders"
    __table_args__ = (UniqueConstraint("token", name="uq_orders_token"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    token: Mapped[int] = mapped_column(Integer, index=True)
    customer_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    pickup_time: Mapped[str] = mapped_column(String(16))
    status: Mapped[str] = mapped_column(String(16), default="New", index=True)
    total: Mapped[int] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
    cooking_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    ready_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    collected_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    prep_minutes: Mapped[int | None] = mapped_column(Integer, nullable=True)
    customer: Mapped[User] = relationship(back_populates="orders")
    items: Mapped[list["OrderItem"]] = relationship(back_populates="order", cascade="all, delete-orphan")
    payment: Mapped["Payment | None"] = relationship(back_populates="order", uselist=False, cascade="all, delete-orphan")


class OrderItem(Base):
    __tablename__ = "order_items"

    id: Mapped[int] = mapped_column(primary_key=True)
    order_id: Mapped[int] = mapped_column(ForeignKey("orders.id", ondelete="CASCADE"), index=True)
    menu_item_id: Mapped[str] = mapped_column(ForeignKey("menu_items.id"))
    item_name: Mapped[str] = mapped_column(String(100))
    unit_price: Mapped[int] = mapped_column(Integer)
    quantity: Mapped[int] = mapped_column(Integer)
    order: Mapped[Order] = relationship(back_populates="items")


class Payment(Base):
    __tablename__ = "payments"

    id: Mapped[int] = mapped_column(primary_key=True)
    order_id: Mapped[int] = mapped_column(ForeignKey("orders.id", ondelete="CASCADE"), unique=True)
    transaction_id: Mapped[str] = mapped_column(String(40), unique=True)
    amount: Mapped[int] = mapped_column(Integer)
    method: Mapped[str] = mapped_column(String(32), default="Paid at pickup")
    status: Mapped[str] = mapped_column(String(16), default="Paid")
    provider_order_id: Mapped[str | None] = mapped_column(String(64), unique=True, nullable=True)
    provider_payment_id: Mapped[str | None] = mapped_column(String(64), unique=True, nullable=True)
    paid_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    order: Mapped[Order] = relationship(back_populates="payment")