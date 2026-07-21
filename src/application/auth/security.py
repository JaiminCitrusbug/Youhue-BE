"""Cryptographic primitives: password hashing (bcrypt), JWT, and at-rest token/code hashing."""
import hashlib
import secrets
from datetime import datetime
from typing import Any

import bcrypt
import jwt

from src.config import settings


def hash_password(plain: str) -> str:
    return str(bcrypt.hashpw(plain.encode(), bcrypt.gensalt()).decode())


def verify_password(plain: str, hashed: str) -> bool:
    try:
        return bool(bcrypt.checkpw(plain.encode(), hashed.encode()))
    except ValueError:
        return False


def hash_secret(raw: str) -> str:
    """SHA-256 for reset tokens / OTP codes stored at rest (not passwords)."""
    return hashlib.sha256(raw.encode()).hexdigest()


def new_url_token() -> str:
    return secrets.token_urlsafe(32)


def new_numeric_code(length: int) -> str:
    return "".join(secrets.choice("0123456789") for _ in range(length))


def encode_jwt(claims: dict[str, Any], expires_at: datetime) -> str:
    payload = {**claims, "exp": expires_at}
    return str(jwt.encode(payload, settings.jwt_secret, algorithm=settings.jwt_algorithm))


def decode_jwt(token: str) -> dict[str, Any]:
    return dict(
        jwt.decode(token, settings.jwt_secret, algorithms=[settings.jwt_algorithm])
    )
