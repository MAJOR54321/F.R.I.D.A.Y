"""Who is acting right now, and who owns a shared physical resource.

With background agents, several "actors" (the main conversation plus N agents) call tools at the same time.
Two things must therefore be attributable and arbitrated:

  * `current_actor`  — a ContextVar naming whoever is running the current tool call ("main" or "agent:a3").
                       asyncio tasks and asyncio.to_thread() both propagate it, so confirmation prompts, logs and
                       permission grants can always say WHO is asking.
  * `ResourceArbiter` — a lease on a resource that can't be shared (the mouse/keyboard). Only one actor may drive it
                       at a time; the lease is released when that actor goes idle, finishes, or dies.
"""
from __future__ import annotations

import threading
import time
from contextvars import ContextVar
from typing import Callable, Optional

MAIN = "main"

current_actor: ContextVar[str] = ContextVar("friday_actor", default=MAIN)


class ResourceArbiter:
    def __init__(self, idle_release_s: float = 45.0, is_alive: Optional[Callable[[str], bool]] = None):
        self.idle_release_s = idle_release_s
        self._is_alive = is_alive or (lambda actor: True)
        self._owners: dict[str, tuple[str, float]] = {}
        self._lock = threading.Lock()

    def acquire(self, resource: str, actor: str) -> Optional[str]:
        """Claim (or refresh) a lease. Returns None on success, or the name of the current owner if it's taken."""
        now = time.time()
        with self._lock:
            current = self._owners.get(resource)
            if current is not None:
                owner, last_used = current
                if owner != actor and now - last_used < self.idle_release_s and self._is_alive(owner):
                    return owner
            self._owners[resource] = (actor, now)
            return None

    def owner(self, resource: str) -> Optional[str]:
        with self._lock:
            current = self._owners.get(resource)
            return current[0] if current else None

    def release_all(self, actor: str) -> None:
        with self._lock:
            for resource, (owner, _) in list(self._owners.items()):
                if owner == actor:
                    del self._owners[resource]
