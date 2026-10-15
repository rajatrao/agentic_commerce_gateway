"""Per-shop in-process cache for rendered llm.txt documents."""

import time


class LlmTxtCache:
    def __init__(self, ttl_seconds: int) -> None:
        self._ttl = ttl_seconds
        self._entries: dict[str, tuple[float, str]] = {}

    def get(self, shop: str) -> str | None:
        entry = self._entries.get(shop)
        if entry is None:
            return None
        stored_at, body = entry
        if time.monotonic() - stored_at > self._ttl:
            self._entries.pop(shop, None)
            return None
        return body

    def put(self, shop: str, body: str) -> None:
        self._entries[shop] = (time.monotonic(), body)

    def invalidate(self, shop: str) -> None:
        self._entries.pop(shop, None)
