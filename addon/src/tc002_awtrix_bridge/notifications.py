from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from typing import Any

from .errors import capacity


@dataclass(slots=True)
class Notification:
    spec: dict[str, Any]
    generation: int
    started_ms: int

    @property
    def name(self) -> str:
        return str(self.spec.get("name", ""))


class NotificationManager:
    """AWTRIX NG FIFO notification semantics, including stack=false replacement."""

    def __init__(self, capacity_limit: int = 32) -> None:
        self.capacity = capacity_limit
        self._queue: deque[Notification] = deque()
        self._generation = 0

    def __len__(self) -> int:
        return len(self._queue)

    @property
    def active(self) -> Notification | None:
        return self._queue[0] if self._queue else None

    def items(self) -> list[dict[str, Any]]:
        return [item.spec.copy() for item in self._queue]

    def push(self, spec: dict[str, Any], now_ms: int) -> Notification:
        self._generation += 1
        item = Notification(spec=spec.copy(), generation=self._generation, started_ms=now_ms)
        if not spec.get("stack", True) and self._queue:
            self._queue[0] = item
            return item
        if len(self._queue) >= self.capacity:
            raise capacity(f"Notification capacity of {self.capacity} reached")
        self._queue.append(item)
        return item

    def dismiss_active(self, now_ms: int) -> bool:
        if not self._queue:
            return False
        self._queue.popleft()
        if self._queue:
            self._generation += 1
            self._queue[0].generation = self._generation
            self._queue[0].started_ms = now_ms
        return True

    def dismiss_named(self, name: str, now_ms: int) -> int:
        if not self._queue:
            return 0
        index = next(
            (position for position, item in enumerate(self._queue) if item.name == name), -1
        )
        if index < 0:
            return 0
        active_removed = index == 0
        del self._queue[index]
        if active_removed and self._queue:
            self._generation += 1
            self._queue[0].generation = self._generation
            self._queue[0].started_ms = now_ms
        return 1

    def should_expire(
        self, now_ms: int, *, scroll_complete: bool, default_duration_ms: int = 7000
    ) -> bool:
        active = self.active
        if active is None or active.spec.get("hold", False):
            return False
        if "durationMs" in active.spec:
            return now_ms - active.started_ms >= int(active.spec["durationMs"])
        if scroll_complete:
            return True
        return now_ms - active.started_ms >= max(0, default_duration_ms)
