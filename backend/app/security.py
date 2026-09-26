import hashlib
import hmac
import secrets
from datetime import datetime, timedelta, timezone

import jwt
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.database import get_db
from app.models import User

bearer = HTTPBearer(auto_error=False)
_HASH_ROUNDS = 310_000


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    derived = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, _HASH_ROUNDS)
    return f"pbkdf2_sha256${_HASH_ROUNDS}${salt.hex()}${derived.hex()}"


def verify_password(password: str, encoded: str) -> bool:
    try:
        algorithm, rounds, salt_hex, digest_hex = encoded.split("$")
        if algorithm != "pbkdf2_sha256":
            return False
        derived = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt_hex), int(rounds))
        return hmac.compare_digest(derived.hex(), digest_hex)
    except (ValueError, TypeError):
        return False


def create_access_token(user: User) -> str:
    settings = get_settings()
    expires = datetime.now(timezone.utc) + timedelta(minutes=settings.access_token_minutes)
    return jwt.encode({"sub": str(user.id), "role": user.role, "exp": expires}, settings.jwt_secret_key, algorithm=settings.jwt_algorithm)


def get_current_user(credentials: HTTPAuthorizationCredentials | None = Depends(bearer), db: Session = Depends(get_db)) -> User:
    unauthorized = HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Sign in to continue.", headers={"WWW-Authenticate": "Bearer"})
    if not credentials:
        raise unauthorized
    try:
        payload = jwt.decode(credentials.credentials, get_settings().jwt_secret_key, algorithms=[get_settings().jwt_algorithm])
        user_id = int(payload.get("sub", ""))
    except (jwt.InvalidTokenError, TypeError, ValueError):
        raise unauthorized from None
    user = db.scalar(select(User).where(User.id == user_id))
    if user is None:
        raise unauthorized
    return user


def require_customer(user: User = Depends(get_current_user)) -> User:
    if user.role not in {"student", "faculty"}:
        raise HTTPException(status_code=403, detail="A student or faculty account is required.")
    return user


def require_admin(user: User = Depends(get_current_user)) -> User:
    if user.role != "admin":
        raise HTTPException(status_code=403, detail="Canteen staff access is required.")
    return user