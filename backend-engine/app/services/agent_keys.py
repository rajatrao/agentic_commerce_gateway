"""API keys for AI agents calling a merchant's UCP / ACP / MCP endpoints.

Format: `acg_<env>_<prefix>_<secret>`, e.g. `acg_live_3f9a1c0b7e2d_V1x...`. The 12-hex-char prefix
is stored in clear (indexed) to find the client row; the whole key is stored only as an argon2
hash. Verified keys are remembered in-process for a few minutes so argon2 does not run on every
request, but the client row (and so revocation) is still read from the database each time.
"""

import hashlib
import re
import secrets
import time
from dataclasses import dataclass

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError

KEY_RE = re.compile(r"^acg_(live|test)_([0-9a-f]{12})_([A-Za-z0-9_-]{32,})$")
VERIFIED_TTL_SECONDS = 300

_hasher = PasswordHasher()


@dataclass(frozen=True)
class IssuedKey:
    """A newly issued key: the plaintext (shown once), its public prefix and its argon2 hash."""

    plaintext: str
    prefix: str
    hash: str


def issue_key(live: bool = True) -> IssuedKey:
    """Generate a new agent API key and its argon2 hash."""
    prefix = secrets.token_hex(6)
    plaintext = f"acg_{'live' if live else 'test'}_{prefix}_{secrets.token_urlsafe(32)}"
    return IssuedKey(plaintext=plaintext, prefix=prefix, hash=_hasher.hash(plaintext))


def key_prefix(plaintext: str) -> str | None:
    """Return the lookup prefix of a well-formed key, or None."""
    match = KEY_RE.fullmatch(plaintext)
    return match.group(2) if match else None


class KeyVerifier:
    """Verifies keys against their argon2 hash, remembering recent successes briefly to skip
    rehashing."""

    def __init__(self, ttl_seconds: int = VERIFIED_TTL_SECONDS) -> None:
        self._ttl = ttl_seconds
        self._verified: dict[str, float] = {}

    def verify(self, plaintext: str, key_hash: str) -> bool:
        """Return whether `plaintext` matches `key_hash`."""
        fingerprint = hashlib.sha256(f"{key_hash}\0{plaintext}".encode()).hexdigest()
        now = time.monotonic()
        if self._verified.get(fingerprint, 0) > now:
            return True
        try:
            _hasher.verify(key_hash, plaintext)
        except (VerificationError, InvalidHashError):
            return False
        if len(self._verified) > 10_000:
            self._verified = {k: v for k, v in self._verified.items() if v > now}
        self._verified[fingerprint] = now + self._ttl
        return True
