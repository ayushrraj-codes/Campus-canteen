import base64
import binascii
import re
from urllib.parse import urlsplit
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


class RegisterRequest(BaseModel):
    name: str = Field(min_length=2, max_length=70)
    email: str = Field(min_length=5, max_length=160)
    password: str = Field(min_length=8, max_length=128)
    role: Literal["student", "faculty"] = "student"
    mobile: str | None = Field(default=None, max_length=16)

    @field_validator("email")
    @classmethod
    def normalize_email(cls, value: str) -> str:
        value = value.strip().lower()
        if "@" not in value or "." not in value.rsplit("@", 1)[-1]:
            raise ValueError("Enter a valid email address.")
        return value

    @field_validator("name")
    @classmethod
    def clean_name(cls, value: str) -> str:
        value = value.strip()
        if len(value) < 2:
            raise ValueError("Name must contain at least 2 characters.")
        return value

    @field_validator("mobile")
    @classmethod
    def normalize_mobile(cls, value: str | None) -> str | None:
        return normalize_mobile(value)


class LoginRequest(BaseModel):
    email: str = Field(min_length=5, max_length=160)
    password: str = Field(min_length=1, max_length=128)

    @field_validator("email")
    @classmethod
    def normalize_email(cls, value: str) -> str:
        return value.strip().lower()


class GoogleAuthRequest(BaseModel):
    credential: str = Field(min_length=20, max_length=8192)
    role: Literal["student", "faculty"] = "student"


class ProfileUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=2, max_length=70)
    mobile: str | None = Field(default=None, max_length=16)
    photo_url: str | None = Field(default=None, max_length=410_000)

    @field_validator("name")
    @classmethod
    def clean_name(cls, value: str | None) -> str | None:
        if value is None:
            return None
        value = value.strip()
        if len(value) < 2:
            raise ValueError("Name must contain at least 2 characters.")
        return value

    @field_validator("mobile")
    @classmethod
    def clean_mobile(cls, value: str | None) -> str | None:
        return normalize_mobile(value)

    @field_validator("photo_url")
    @classmethod
    def validate_photo(cls, value: str | None) -> str | None:
        if value is None:
            return None
        if value.startswith("https://"):
            parsed = urlsplit(value)
            if parsed.hostname and not parsed.username and not parsed.password:
                return value
            raise ValueError("Photo URL must be a valid HTTPS address.")
        match = re.fullmatch(r"data:image/(jpeg|png|webp);base64,([A-Za-z0-9+/]+={0,2})", value)
        if not match:
            raise ValueError("Upload a JPEG, PNG, or WebP image.")
        try:
            image = base64.b64decode(match.group(2), validate=True)
        except (binascii.Error, ValueError):
            raise ValueError("The uploaded photo is not valid base64 image data.") from None
        if not image or len(image) > 300_000:
            raise ValueError("Profile photos must be smaller than 300 KB.")
        content_type = match.group(1)
        valid_image = {
            "jpeg": image.startswith(b"\xff\xd8\xff"),
            "png": image.startswith(b"\x89PNG\r\n\x1a\n"),
            "webp": image.startswith(b"RIFF") and image[8:12] == b"WEBP",
        }[content_type]
        if not valid_image:
            raise ValueError("The uploaded file does not match its image type.")
        return value


def normalize_mobile(value: str | None) -> str | None:
    if value is None or not value.strip():
        return None
    normalized = re.sub(r"[\s()-]", "", value)
    if not re.fullmatch(r"\+?[0-9]{10,15}", normalized):
        raise ValueError("Enter a mobile number with 10 to 15 digits, optionally starting with +.")
    return normalized


class OrderLineRequest(BaseModel):
    item_id: str = Field(min_length=1, max_length=64)
    quantity: int = Field(ge=1, le=20)


class CreateOrderRequest(BaseModel):
    items: list[OrderLineRequest] = Field(min_length=1, max_length=20)
    pickup_time: str
    payment_method: Literal["cash", "upi"] = "cash"

    @field_validator("pickup_time")
    @classmethod
    def valid_pickup_time(cls, value: str) -> str:
        allowed = {"11:30 AM", "11:45 AM", "12:00 PM", "12:15 PM", "12:30 PM", "12:45 PM", "1:00 PM", "1:15 PM", "1:30 PM", "1:45 PM", "2:00 PM", "2:15 PM", "2:30 PM"}
        if value not in allowed:
            raise ValueError("Choose a listed pickup time between 11:30 AM and 2:30 PM.")
        return value


class VerifyPaymentRequest(BaseModel):
    razorpay_order_id: str = Field(min_length=8, max_length=64)
    razorpay_payment_id: str = Field(min_length=8, max_length=64)
    razorpay_signature: str = Field(min_length=32, max_length=128)


class UpiReferenceRequest(BaseModel):
    reference: str = Field(min_length=8, max_length=40)

    @field_validator("reference")
    @classmethod
    def normalize_reference(cls, value: str) -> str:
        value = value.strip().upper()
        if not re.fullmatch(r"[A-Z0-9-]{8,40}", value):
            raise ValueError("Enter the UPI transaction reference shown by your payment app.")
        return value


class StatusUpdate(BaseModel):
    status: Literal["Cooking", "Ready", "Collected"]


class AvailabilityUpdate(BaseModel):
    available: bool


class UserResponse(BaseModel):
    id: int
    email: str
    name: str
    role: str
    model_config = ConfigDict(from_attributes=True)