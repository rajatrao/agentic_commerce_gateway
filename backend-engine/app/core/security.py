"""Cryptographic primitives: service-JWT verification, Shopify HMAC and token encryption."""

import base64
import hashlib
import hmac
from dataclasses import dataclass
from datetime import UTC, datetime

import jwt
from cryptography.fernet import Fernet, InvalidToken

from app.core.config import Settings


class AuthError(Exception):
    """Raised when a credential is missing, malformed or invalid."""


@dataclass(frozen=True)
class ServiceClaims:
    shop: str
    scopes: frozenset[str]
    jti: str
    expires_at: datetime


def verify_service_jwt(token: str, settings: Settings) -> ServiceClaims:
    """Verify a JWT minted by frontend-ui (EdDSA / Ed25519)."""
    if not settings.jwt_public_key:
        raise AuthError("JWT public key not configured")
    try:
        payload = jwt.decode(
            token,
            settings.jwt_public_key,
            algorithms=["EdDSA"],
            audience=settings.jwt_audience,
            issuer=settings.jwt_issuer,
            leeway=settings.jwt_leeway_seconds,
            options={"require": ["exp", "iat", "sub", "jti", "aud", "iss"]},
        )
    except jwt.PyJWTError as exc:
        raise AuthError(f"invalid service token: {exc}") from exc

    scope = payload.get("scope", [])
    if isinstance(scope, str):
        scope = scope.split()
    return ServiceClaims(
        shop=payload["sub"],
        scopes=frozenset(scope),
        jti=payload["jti"],
        expires_at=datetime.fromtimestamp(payload["exp"], tz=UTC),
    )


class ReplayCache:
    """In-process `jti` replay guard. Swap for Redis when running more than one replica."""

    def __init__(self) -> None:
        self._seen: dict[str, datetime] = {}

    def check_and_add(self, jti: str, expires_at: datetime) -> bool:
        now = datetime.now(UTC)
        if len(self._seen) > 10_000:
            self._seen = {k: v for k, v in self._seen.items() if v > now}
        if jti in self._seen:
            return False
        self._seen[jti] = expires_at
        return True


def verify_shopify_webhook_hmac(raw_body: bytes, hmac_header: str | None, secret: str) -> bool:
    if not hmac_header or not secret:
        return False
    digest = hmac.new(secret.encode(), raw_body, hashlib.sha256).digest()
    expected = base64.b64encode(digest).decode()
    return hmac.compare_digest(expected, hmac_header)


class TokenCipher:
    """Fernet encryption for Shopify offline access tokens stored in Postgres."""

    def __init__(self, key: str) -> None:
        if not key:
            raise ValueError("TOKEN_ENCRYPTION_KEY is not configured")
        self._fernet = Fernet(key.encode())

    def encrypt(self, plaintext: str) -> bytes:
        return self._fernet.encrypt(plaintext.encode())

    def decrypt(self, ciphertext: bytes) -> str:
        try:
            return self._fernet.decrypt(ciphertext).decode()
        except InvalidToken as exc:
            raise AuthError("stored token could not be decrypted") from exc
