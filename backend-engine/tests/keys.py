"""Helpers that mimic frontend-ui's service-JWT minting (see frontend-ui/app/lib/jwt.server.ts)."""

import time
import uuid
from typing import Any

import jwt
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey


def make_keypair() -> tuple[str, str]:
    key = Ed25519PrivateKey.generate()
    private_pem = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode()
    public_pem = (
        key.public_key()
        .public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo)
        .decode()
    )
    return private_pem, public_pem


def mint(
    private_pem: str,
    shop: str,
    scope: list[str] | None = None,
    **overrides: Any,
) -> str:
    now = int(time.time())
    payload: dict[str, Any] = {
        "iss": "frontend-ui",
        "aud": "backend-engine",
        "sub": shop,
        "scope": scope if scope is not None else ["merchant:read", "merchant:write"],
        "iat": now,
        "exp": now + 60,
        "jti": str(uuid.uuid4()),
    }
    payload.update(overrides)
    return jwt.encode(payload, private_pem, algorithm="EdDSA")
