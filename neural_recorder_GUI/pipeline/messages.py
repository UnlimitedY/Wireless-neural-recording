"""Typed messages shared by the serial processing pipeline."""

from __future__ import annotations

from dataclasses import dataclass, field
import time
from typing import Any, Mapping, Optional, Tuple


def _now_epoch() -> float:
    return time.time()


@dataclass(frozen=True)
class RawFrame:
    """A raw byte frame received from the serial reader."""

    data: bytes
    timestamp_epoch: float = field(default_factory=_now_epoch)
    source: str = "serial"
    sequence: int = 0
    metadata: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class DecodedPacketBatch:
    """A batch of decoded packets produced from one or more raw frames."""

    packets: Tuple[Any, ...]
    timestamp_epoch: float = field(default_factory=_now_epoch)
    source_sequence: Optional[int] = None
    sequence: int = 0
    metadata: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ProcessedStreamChunk:
    """Processed neural samples ready for GUI display or persistence."""

    timestamps: Tuple[float, ...]
    channels: Tuple[Tuple[float, ...], ...]
    sample_rate_hz: Optional[float] = None
    timestamp_epoch: float = field(default_factory=_now_epoch)
    sequence: int = 0
    metadata: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class GuiPayload:
    """A GUI-facing event payload with an optional coalescing key."""

    kind: str
    payload: Any
    timestamp_epoch: float = field(default_factory=_now_epoch)
    sequence: int = 0
    coalesce_key: Optional[str] = None
    metadata: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class SaveChunk:
    """A persistence-facing event payload."""

    payload: Any
    timestamp_epoch: float = field(default_factory=_now_epoch)
    sequence: int = 0
    path_hint: Optional[str] = None
    metadata: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class PipelineStatus:
    """Runtime status snapshot for monitoring and tests."""

    running: bool
    worker_alive: bool
    raw_queue_depth: int
    event_queue_depth: int
    submitted_frames: int
    processed_frames: int
    emitted_events: int
    dropped_raw_frames: int
    dropped_events: int
    coalesced_events: int
    last_error: Optional[str] = None
    started_at_epoch: Optional[float] = None
    stopped_at_epoch: Optional[float] = None

