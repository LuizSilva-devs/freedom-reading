"""Limite de tentativas em memória (janela deslizante).

Protege login e cadastro contra tentativas de senha em massa. Fica na memória
do processo: suficiente para um servidor único (EC2 + uvicorn). Com vários
servidores, o ideal seria um armazenamento compartilhado (ex.: Redis).
"""
import time
from collections import deque
from threading import Lock

_hits: dict[str, deque] = {}
_lock = Lock()


def allow(key: str, limit: int, window_seconds: int) -> bool:
    """Registra uma tentativa e diz se ela ainda está dentro do limite."""
    now = time.monotonic()
    with _lock:
        if len(_hits) > 10000:  # proteção contra crescimento infinito
            _hits.clear()
        q = _hits.setdefault(key, deque())
        while q and now - q[0] > window_seconds:
            q.popleft()
        if len(q) >= limit:
            return False
        q.append(now)
        return True


def is_blocked(key: str, limit: int, window_seconds: int) -> bool:
    """Diz se a chave já estourou o limite, sem registrar uma nova tentativa."""
    now = time.monotonic()
    with _lock:
        q = _hits.get(key)
        if not q:
            return False
        while q and now - q[0] > window_seconds:
            q.popleft()
        return len(q) >= limit


def reset(key: str) -> None:
    with _lock:
        _hits.pop(key, None)


def clear() -> None:
    with _lock:
        _hits.clear()
