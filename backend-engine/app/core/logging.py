"""Structured logging with redaction of secrets and payment tokens."""

import logging
import re
from collections.abc import MutableMapping
from typing import Any

import structlog

_SENSITIVE_KEYS = {
    "access_token",
    "accesstoken",
    "authorization",
    "password",
    "secret",
    "token",
    "payment_token",
    "shared_payment_token",
    "card",
}
# Stripe shared payment tokens, secret keys and bearer tokens.
_SENSITIVE_PATTERNS = re.compile(r"\b(spt_|sk_live_|sk_test_|shpat_|shpca_|Bearer\s+)[\w\-.]+")


def _redact(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            k: "[REDACTED]" if k.lower() in _SENSITIVE_KEYS else _redact(v)
            for k, v in value.items()
        }
    if isinstance(value, list | tuple):
        return type(value)(_redact(v) for v in value)
    if isinstance(value, str):
        return _SENSITIVE_PATTERNS.sub(lambda m: m.group(1) + "[REDACTED]", value)
    return value


def redact_processor(
    _logger: Any, _method: str, event_dict: MutableMapping[str, Any]
) -> MutableMapping[str, Any]:
    return _redact(dict(event_dict))  # type: ignore[no-any-return]


def configure_logging(level: str = "INFO", json_logs: bool = True) -> None:
    logging.basicConfig(format="%(message)s", level=level)
    renderer = structlog.processors.JSONRenderer() if json_logs else structlog.dev.ConsoleRenderer()
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso"),
            redact_processor,
            renderer,
        ],
        wrapper_class=structlog.make_filtering_bound_logger(logging.getLevelName(level)),
    )
