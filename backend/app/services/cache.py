"""Cache simples em memória com expiração (evita repetir chamadas às APIs públicas)."""
import time
from threading import Lock
from typing import Any

_store: dict[str, tuple[float, Any]] = {}
_lock = Lock()
_MISS = object()


def get(key: str) -> Any:
    with _lock:
        entry = _store.get(key)
        if not entry:
            return _MISS
        expires, value = entry
        if time.time() > expires:
            _store.pop(key, None)
            return _MISS
        return value


def set(key: str, value: Any, ttl: int = 600) -> None:
    with _lock:
        if len(_store) > 2000:  # proteção contra crescimento infinito
            _store.clear()
        _store[key] = (time.time() + ttl, value)


def is_miss(value: Any) -> bool:
    return value is _MISS


def clear() -> None:
    with _lock:
        _store.clear()
