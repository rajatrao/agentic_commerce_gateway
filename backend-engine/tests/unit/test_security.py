import base64
import hashlib
import hmac
import time
import uuid

import jwt
import pytest
from cryptography.fernet import Fernet

from app.core.config import Settings
from app.core.security import (
    AuthError,
    TokenCipher,
    verify_service_jwt,
    verify_shopify_webhook_hmac,
)
from tests.keys import make_keypair, mint


def test_valid_token_roundtrip() -> None:
    private_pem, public_pem = make_keypair()
    settings = Settings(jwt_public_key=public_pem)
    claims = verify_service_jwt(mint(private_pem, "acme.myshopify.com"), settings)
    assert claims.shop == "acme.myshopify.com"
    assert claims.scopes == {"merchant:read", "merchant:write"}


@pytest.mark.parametrize(
    "overrides",
    [
        {"aud": "someone-else"},
        {"iss": "attacker"},
        {"exp": int(time.time()) - 120},
    ],
)
def test_rejects_bad_claims(overrides: dict) -> None:  # type: ignore[type-arg]
    private_pem, public_pem = make_keypair()
    settings = Settings(jwt_public_key=public_pem)
    with pytest.raises(AuthError):
        verify_service_jwt(mint(private_pem, "acme.myshopify.com", **overrides), settings)


def test_rejects_token_signed_by_other_key() -> None:
    other_private, _ = make_keypair()
    _, public_pem = make_keypair()
    with pytest.raises(AuthError):
        verify_service_jwt(
            mint(other_private, "a.myshopify.com"), Settings(jwt_public_key=public_pem)
        )


def test_rejects_hs256_algorithm_confusion() -> None:
    _, public_pem = make_keypair()
    token = jwt.encode(
        {
            "sub": "a.myshopify.com",
            "aud": "backend-engine",
            "iss": "frontend-ui",
            "iat": int(time.time()),
            "exp": int(time.time()) + 60,
            "jti": str(uuid.uuid4()),
        },
        "not-the-key",
        algorithm="HS256",
    )
    with pytest.raises(AuthError):
        verify_service_jwt(token, Settings(jwt_public_key=public_pem))


def test_env_style_escaped_newlines_are_accepted() -> None:
    private_pem, public_pem = make_keypair()
    settings = Settings(jwt_public_key=public_pem.replace("\n", "\\n"))
    assert verify_service_jwt(mint(private_pem, "a.myshopify.com"), settings).shop


def test_shopify_hmac() -> None:
    body = b'{"id":1}'
    sig = base64.b64encode(hmac.new(b"shh", body, hashlib.sha256).digest()).decode()
    assert verify_shopify_webhook_hmac(body, sig, "shh")
    assert not verify_shopify_webhook_hmac(body + b" ", sig, "shh")
    assert not verify_shopify_webhook_hmac(body, None, "shh")


def test_token_cipher_roundtrip() -> None:
    cipher = TokenCipher(Fernet.generate_key().decode())
    assert cipher.decrypt(cipher.encrypt("shpat_abc")) == "shpat_abc"
    with pytest.raises(AuthError):
        TokenCipher(Fernet.generate_key().decode()).decrypt(cipher.encrypt("x"))
