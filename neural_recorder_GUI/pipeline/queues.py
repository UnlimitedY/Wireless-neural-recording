"""Small queue primitives for the serial data pipeline."""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
import queue
import threading
import time
from typing import Any, Callable, Deque, Iterable, List, Optional

from neural_recorder_GUI.storage.shared_memory_payload import (
    pack_for_queue as _pack_for_queue,
    unpack_from_queue as _unpack_from_queue,
)


CoalesceKey = Callable[[Any], Optional[Any]]


@dataclass(frozen=True)
class QueueTelemetry:
    capacity: int
    depth: int
    submitted: int
    accepted: int
    dropped: int
    coalesced: int
    drained: int


def pack_for_queue(message: Any, threshold_bytes: int = 65536) -> Any:
    """Pack large queue messages via the existing shared-memory helper."""

    return _pack_for_queue(message, threshold_bytes=threshold_bytes)


def unpack_from_queue(message: Any) -> Any:
    """Unpack messages produced by :func:`pack_for_queue`."""

    return _unpack_from_queue(message)


def unpack_for_queue(message: Any) -> Any:
    """Backward-friendly alias for code that pairs pack/unpack names."""

    return unpack_from_queue(message)


class BoundedCoalescingQueue:
    """Thread-safe bounded FIFO queue with optional key-based coalescing."""

    def __init__(
        self,
        maxsize: int,
        coalesce_key: Optional[CoalesceKey] = None,
        *,
        drop_oldest: bool = True,
    ) -> None:
        if int(maxsize) <= 0:
            raise ValueError("maxsize must be positive")
        self.maxsize = int(maxsize)
        self._coalesce_key = coalesce_key
        self._drop_oldest = bool(drop_oldest)
        self._items: Deque[Any] = deque()
        self._condition = threading.Condition()
        self._submitted = 0
        self._accepted = 0
        self._dropped = 0
        self._coalesced = 0
        self._drained = 0

    def put(self, item: Any, block: bool = False, timeout: Optional[float] = None) -> bool:
        """Insert an item, returning False only when the new item is dropped."""

        with self._condition:
            self._submitted += 1
            if self._try_coalesce_locked(item):
                self._accepted += 1
                self._coalesced += 1
                self._condition.notify()
                return True

            deadline = None if timeout is None else time.monotonic() + max(0.0, float(timeout))
            while len(self._items) >= self.maxsize:
                if self._drop_oldest:
                    self._items.popleft()
                    self._dropped += 1
                    break
                if not block:
                    self._dropped += 1
                    return False
                if timeout is None:
                    self._condition.wait()
                    continue
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    self._dropped += 1
                    return False
                self._condition.wait(remaining)

            self._items.append(item)
            self._accepted += 1
            self._condition.notify()
            return True

    def get(self, timeout: Optional[float] = None) -> Any:
        """Remove and return the oldest item, raising queue.Empty on timeout."""

        deadline = None if timeout is None else time.monotonic() + max(0.0, timeout)
        with self._condition:
            while not self._items:
                if timeout is None:
                    self._condition.wait()
                    continue
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise queue.Empty
                self._condition.wait(remaining)
            self._drained += 1
            item = self._items.popleft()
            self._condition.notify()
            return item

    def drain(self, max_items: Optional[int] = None) -> List[Any]:
        with self._condition:
            count = len(self._items) if max_items is None else min(len(self._items), int(max_items))
            items = [self._items.popleft() for _ in range(max(0, count))]
            self._drained += len(items)
            if items:
                self._condition.notify_all()
            return items

    def clear(self) -> None:
        with self._condition:
            dropped = len(self._items)
            self._items.clear()
            self._dropped += dropped
            if dropped:
                self._condition.notify_all()

    def qsize(self) -> int:
        with self._condition:
            return len(self._items)

    def empty(self) -> bool:
        return self.qsize() == 0

    def telemetry(self) -> QueueTelemetry:
        with self._condition:
            return QueueTelemetry(
                capacity=self.maxsize,
                depth=len(self._items),
                submitted=self._submitted,
                accepted=self._accepted,
                dropped=self._dropped,
                coalesced=self._coalesced,
                drained=self._drained,
            )

    def extend(self, items: Iterable[Any]) -> int:
        accepted = 0
        for item in items:
            if self.put(item):
                accepted += 1
        return accepted

    def _try_coalesce_locked(self, item: Any) -> bool:
        if self._coalesce_key is None:
            return False
        key = self._coalesce_key(item)
        if key is None:
            return False
        for index, existing in enumerate(self._items):
            if self._coalesce_key(existing) == key:
                self._items[index] = item
                return True
        return False
