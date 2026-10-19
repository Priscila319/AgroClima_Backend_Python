from __future__ import annotations

import hashlib
import time
from typing import Callable, Generic, TypeVar

V = TypeVar("V")


def token_key(authorization: str) -> str:
    """Chave do cache: hash do header Authorization (o token em si nunca fica guardado em memória)."""
    return hashlib.sha256(authorization.strip().encode("utf-8")).hexdigest()


class TTLCache(Generic[V]):
    """Cache em memória com expiração curta e tamanho máximo.

    Sem locks de propósito: get/set não têm ``await``, então são atômicos no event loop do asyncio.
    ``ttl_seconds <= 0`` desativa o cache (get sempre devolve None, set não guarda nada).
    """

    def __init__(self, ttl_seconds: float, max_entries: int = 1024,
                 clock: Callable[[], float] = time.monotonic):
        self.ttl_seconds = ttl_seconds
        self.max_entries = max_entries
        self._clock = clock
        self._items: dict[str, tuple[float, V]] = {}

    @property
    def enabled(self) -> bool:
        return self.ttl_seconds > 0

    def get(self, key: str) -> V | None:
        entry = self._items.get(key)
        if entry is None:
            return None
        expires_at, value = entry
        if self._clock() >= expires_at:
            self._items.pop(key, None)
            return None
        return value

    def set(self, key: str, value: V) -> None:
        if not self.enabled:
            return
        self._items.pop(key, None)  # reinserir move a chave para o fim (mais recente)
        self._items[key] = (self._clock() + self.ttl_seconds, value)
        if len(self._items) > self.max_entries:
            self._evict()

    def clear(self) -> None:
        self._items.clear()

    def __len__(self) -> int:
        return len(self._items)

    def _evict(self) -> None:
        now = self._clock()
        for key in [k for k, (expires_at, _) in self._items.items() if expires_at <= now]:
            del self._items[key]
        while len(self._items) > self.max_entries:
            del self._items[next(iter(self._items))]  # remove a mais antiga
