"""Threaded serial data pipeline runtime."""

from __future__ import annotations

import queue
import threading
import time
import traceback
from typing import Any, Callable, Iterable, List, Optional, Union

from .messages import GuiPayload, PipelineStatus, RawFrame
from .queues import BoundedCoalescingQueue, pack_for_queue, unpack_from_queue


ProcessorCallback = Callable[[RawFrame], Optional[Iterable[Any]]]


def _event_coalesce_key(event: Any) -> Optional[Any]:
    return getattr(event, "coalesce_key", None)


class SerialDataPipeline:
    """Worker-backed pipeline from raw serial frames to typed output events."""

    def __init__(
        self,
        processor: ProcessorCallback,
        *,
        raw_queue_capacity: int = 512,
        event_queue_capacity: int = 512,
        worker_poll_seconds: float = 0.05,
        raw_queue_put_timeout_seconds: Optional[float] = 0.25,
        pack_threshold_bytes: Optional[int] = None,
    ) -> None:
        self.processor = processor
        self.raw_queue = BoundedCoalescingQueue(raw_queue_capacity, drop_oldest=False)
        self.event_queue = BoundedCoalescingQueue(event_queue_capacity, coalesce_key=_event_coalesce_key)
        self.worker_poll_seconds = float(worker_poll_seconds)
        self.raw_queue_put_timeout_seconds = raw_queue_put_timeout_seconds
        self.pack_threshold_bytes = pack_threshold_bytes
        self._lock = threading.Lock()
        self._stop_event = threading.Event()
        self._worker: Optional[threading.Thread] = None
        self._submitted_frames = 0
        self._processed_frames = 0
        self._emitted_events = 0
        self._sequence = 0
        self._last_error: Optional[str] = None
        self._started_at_epoch: Optional[float] = None
        self._stopped_at_epoch: Optional[float] = None

    def start(self) -> None:
        with self._lock:
            if self._worker is not None and self._worker.is_alive():
                return
            self._stop_event.clear()
            self._started_at_epoch = time.time()
            self._stopped_at_epoch = None
            self._worker = threading.Thread(target=self._run_worker, name="SerialDataPipeline", daemon=True)
            self._worker.start()

    def stop(self, timeout: Optional[float] = 2.0) -> None:
        worker: Optional[threading.Thread]
        with self._lock:
            self._stop_event.set()
            worker = self._worker
        if worker is not None:
            worker.join(timeout=timeout)
        with self._lock:
            self._stopped_at_epoch = time.time()

    def submit_frame(self, frame: Union[RawFrame, bytes, bytearray, memoryview]) -> bool:
        raw_frame = self._coerce_raw_frame(frame)
        accepted = self.raw_queue.put(
            raw_frame,
            block=self.raw_queue_put_timeout_seconds is not None,
            timeout=self.raw_queue_put_timeout_seconds,
        )
        if accepted:
            with self._lock:
                self._submitted_frames += 1
        return accepted

    def submit_raw_frame(self, frame: Union[RawFrame, bytes, bytearray, memoryview]) -> bool:
        """Compatibility alias used by serial-reader integration tests."""

        return self.submit_frame(frame)

    def drain_events(self, max_items: Optional[int] = None) -> List[Any]:
        events = self.event_queue.drain(max_items=max_items)
        return [unpack_from_queue(event) for event in events]

    def status(self) -> PipelineStatus:
        raw_telemetry = self.raw_queue.telemetry()
        event_telemetry = self.event_queue.telemetry()
        worker = self._worker
        with self._lock:
            return PipelineStatus(
                running=not self._stop_event.is_set() and worker is not None and worker.is_alive(),
                worker_alive=worker is not None and worker.is_alive(),
                raw_queue_depth=raw_telemetry.depth,
                event_queue_depth=event_telemetry.depth,
                submitted_frames=self._submitted_frames,
                processed_frames=self._processed_frames,
                emitted_events=self._emitted_events,
                dropped_raw_frames=raw_telemetry.dropped,
                dropped_events=event_telemetry.dropped,
                coalesced_events=event_telemetry.coalesced,
                last_error=self._last_error,
                started_at_epoch=self._started_at_epoch,
                stopped_at_epoch=self._stopped_at_epoch,
            )

    def _coerce_raw_frame(self, frame: Union[RawFrame, bytes, bytearray, memoryview]) -> RawFrame:
        if isinstance(frame, RawFrame):
            return frame
        with self._lock:
            self._sequence += 1
            sequence = self._sequence
        return RawFrame(data=bytes(frame), sequence=sequence)

    def _run_worker(self) -> None:
        while not self._stop_event.is_set() or not self.raw_queue.empty():
            try:
                raw_frame = self.raw_queue.get(timeout=self.worker_poll_seconds)
            except queue.Empty:
                continue

            try:
                events = self.processor(raw_frame) or ()
                emitted = self._emit_events(events)
                with self._lock:
                    self._processed_frames += 1
                    self._emitted_events += emitted
            except Exception:
                with self._lock:
                    self._last_error = traceback.format_exc()
                self._emit_events(
                    [
                        GuiPayload(
                            kind="pipeline_error",
                            payload={"error": self._last_error},
                            coalesce_key="pipeline_error",
                        )
                    ]
                )

    def _emit_events(self, events: Iterable[Any]) -> int:
        emitted = 0
        for event in events:
            queued_event = (
                pack_for_queue(event, threshold_bytes=self.pack_threshold_bytes)
                if self.pack_threshold_bytes is not None
                else event
            )
            if self.event_queue.put(queued_event):
                emitted += 1
        return emitted


__all__ = ["SerialDataPipeline", "ProcessorCallback"]
