"""Cryptographic primitives: password hashing (bcrypt), JWT, and at-rest token/code hashing."""
import base64
import hashlib
import secrets
from datetime import datetime
from typing import Any

import bcrypt
import jwt

from config.env_config import settings


def _prehash(plain: str) -> bytes:
    """base64(sha256) — keeps bcrypt input <= 44 bytes: no 72-byte truncation, no NUL bytes."""
    return base64.b64encode(hashlib.sha256(plain.encode()).digest())


def hash_password(plain: str) -> str:
    return str(bcrypt.hashpw(_prehash(plain), bcrypt.gensalt()).decode())


def verify_password(plain: str, hashed: str) -> bool:
    try:
        return bool(bcrypt.checkpw(_prehash(plain), hashed.encode()))
    except ValueError:
        return False


# Constant-time target: sign-in verifies against this when no password account exists, so an
# unknown/SSO-only email costs the same bcrypt time as a real one (no user-enumeration timing leak).
DUMMY_PASSWORD_HASH = hash_password(secrets.token_urlsafe(16))


def hash_secret(raw: str) -> str:
    """SHA-256 for reset tokens / OTP codes stored at rest (not passwords)."""
    return hashlib.sha256(raw.encode()).hexdigest()


def new_url_token() -> str:
    return secrets.token_urlsafe(32)


def new_numeric_code(length: int) -> str:
    return "".join(secrets.choice("0123456789") for _ in range(length))


# Unambiguous alphabet for human-typed class join codes (no 0/O/1/I to avoid mis-keying).
_JOIN_CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"


def new_join_code(length: int = 6) -> str:
    """Short, human-typable class join code (FR-01-02); cryptographically random."""
    return "".join(secrets.choice(_JOIN_CODE_ALPHABET) for _ in range(length))


def new_school_code(length: int = 8) -> str:
    """School sign-in code students enter at sign-in (FR-02-01). Same unambiguous alphabet as the
    class join code; longer default for a school-wide, globally-unique code."""
    return "".join(secrets.choice(_JOIN_CODE_ALPHABET) for _ in range(length))


def encode_jwt(claims: dict[str, Any], expires_at: datetime) -> str:
    payload = {**claims, "exp": expires_at}
    return str(jwt.encode(payload, settings.jwt_secret, algorithm=settings.jwt_algorithm))


def decode_jwt(token: str) -> dict[str, Any]:
    return dict(
        jwt.decode(token, settings.jwt_secret, algorithms=[settings.jwt_algorithm])
    )
