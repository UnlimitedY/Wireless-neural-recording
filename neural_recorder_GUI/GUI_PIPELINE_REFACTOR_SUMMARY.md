# GUI Pipeline Refactor Summary

## Scope

This note checks frontend-visible behavior and key logic parity for the new
serial data pipeline. It is intentionally limited to the GUI-facing path,
queue/process model, tests, and hardware validation items. It has been updated
after the block-driven chart refresh and queue policy pass.

## Before Data Flow

Legacy serial flow:

1. `SerialPort(QThread)` opened the neural serial port and ran `read_data()`.
2. `read_data()` accumulated bytes until a complete raw frame ended with
   `%&'(`.
3. The same serial thread called `data_process_full(full_frame)`.
4. The same path then called `data_file_saving_control()` and
   `GUIUpate_enable()`.
5. `GUIUpate_enable()` emitted the existing Qt signals:
   `GUIUpdate`, `ThresholdSamplesUpdate`, `ProgressUpdate`, `StatusUpdate`,
   impedance signals, and disconnect/status side effects.
6. Save work was handed to `save_process_main` through `save_queue`.

This meant serial reading, frame decode, save enqueue, and GUI payload handoff
were coupled inside the serial read thread.

## After Data Flow

Current pipeline flow:

1. `SerialPort(QThread)` still owns the serial port and still accumulates a
   full raw frame ending with `%&'(`.
2. `read_data()` submits the completed frame to `SerialDataPipeline` through
   `_submit_raw_frame_to_pipeline()`.
3. `SerialDataPipeline` stores raw frames in a bounded queue and processes
   them on its daemon worker thread.
4. The worker callback is `SerialPort._process_pipeline_raw_frame()`, which
   calls `_process_complete_frame(frame)`.
5. `_process_complete_frame()` preserves the legacy processing order:
   `data_process_full(frame)`, then `data_file_saving_control()`, then
   `GUIUpate_enable()`, under `self._data_lock`.
6. `read_data()` drains pipeline events opportunistically while the serial loop
   is active. Pipeline errors are surfaced into `last_error` and force a
   `StatusUpdate`.
7. If pipeline construction is unavailable, `_submit_raw_frame_to_pipeline()`
   falls back to `_process_complete_frame(frame)`.
8. GUI chart payloads are now controlled by accumulated data block counts and
   the dynamic GUI interval state. The default runtime configuration is
   block-driven; the project runtime JSON can still override detail polling and
   render intervals for compatibility. Each cage keeps independent
   `gui_update_intervals_by_mode` / stream interval settings, refreshes loss
   statistics, and persists the current interval/stats into the cage profile
   JSON.
9. Serial read performance is tracked as the `read_all()` byte count. A
   bias-corrected EWMA is computed separately for mode0-mode3 every 10 seconds,
   then surfaced in Master Console as four read metrics.

The visible GUI path therefore still depends on the legacy decode/save/gui
methods; the refactor moves frame processing off the serial drain loop rather
than replacing frontend payload semantics.

## Preserved GUI Behavior

- Existing Qt signal names and payload shapes are preserved for chart/detail
  consumers:
  - mode 0/3 LFP payload: `[[0, reref], timestamps, lfp, sensors, raster, esa, packet_indices]`
  - mode 1 payload: `[[1, channel], timestamps, samples, sensors, raster, alignment, packet_indices]`
  - mode 2 payload: `[[2], timestamps, channel_data, sensors, packet_indices]`
  - threshold samples payload: `{"reported_fw_channel": ..., "samples": ...}`
- GUI payload handoff remains in `GUIUpate_enable()`. The update threshold is
  data-block based and can adapt per cage/per mode from packet loss; runtime
  JSON may still impose detail-view polling/render throttles without changing
  the visible GUI workflow.
- Detail chart backpressure remains frontend-visible through status fields:
  `detail_throttled`, `detail_dropped_frames`, and
  `detail_last_skip_reason`.
- Packet loss, packet gap, serial backlog, serial read gap, battery triplet,
  save progress, reref mode, selected channels, and save flags continue to be
  emitted via `StatusUpdate`.
- New pipeline telemetry is additive under `StatusUpdate["pipeline"]`; existing
  status keys are not removed.
- Master Console and `SlotService` external behavior is expected to remain
  unchanged: commands still flow through slot commands/controllers, while
  neural chart/status data still arrives through the existing neural controller
  status/detail surfaces.

## Queue And Process Model

- Master Console remains one UI process.
- Each cage remains an independent `SlotService` process with its own command
  queue, runtime cache, controllers, serial resources, camera resources, and
  writer resources.
- Each `SerialPort` remains a `QThread` inside its owning process and still owns
  serial I/O.
- New per-serial pipeline:
  - raw frame queue: `BoundedCoalescingQueue(maxsize=4096, drop_oldest=False)`
    with bounded wait on enqueue
  - event queue: `BoundedCoalescingQueue(maxsize=128)` with optional event
    coalescing by `GuiPayload.coalesce_key`
  - worker thread: daemon `SerialDataPipeline` thread
  - status counters: submitted/processed/emitted/dropped/coalesced frames/events
  - detail-chart backpressure is applied when raw queue depth grows, so display
    frames are throttled before acquisition/save frames are sacrificed
- Save queue:
  - `SerialPort.save_queue` is now an unbounded `multiprocessing.Queue()`.
  - `_save_put()` uses blocking enqueue to prioritize EDF data preservation over
    dropping chunks.
  - Large save messages are packed through `pack_for_queue(...,
    prefer_shared_memory=False)` before enqueueing, so the writer process
    receives file-backed payload references instead of Windows shared-memory
    names that can expire before `OpenFileMapping`.
  - Enqueue or decode failure increments/logs telemetry where possible, updates
    `last_save_enqueue_ms` / `last_save_enqueue_type`, and forces throttled
    status updates.
- Habits SD download files are now written through a background text writer
  queue. Serial callbacks enqueue open/write/close operations instead of doing
  file I/O inline.
- LFP chart refresh no longer drives raster redraws; raster rendering stride is
  2 for the spike/raster paths that still render raster data.
- Runtime cache writes keep retrying after transient Windows sharing violations
  and only write a System Log warning after repeated failures, so a single
  antivirus/sync-reader lock does not appear as an error.

## Tests Run

Focused parity and pipeline tests:

- `tests/test_pipeline_queues.py`
- `tests/test_neural_reader_pipeline_integration.py`
- `tests/test_runtime_cache_and_shared_payload.py`
- `tests/test_habits_controller_status.py`
- `tests/test_master_console_contracts.py`
- `tests/test_master_console_ui.py`
- `tests/test_master_console_nonblocking.py`
- `tests/test_slot_service_commands.py`

Command:

```bash
env QT_QPA_PLATFORM=offscreen .venv-master-console/bin/python -m pytest -q tests/test_pipeline_queues.py tests/test_neural_reader_pipeline_integration.py tests/test_master_console_nonblocking.py tests/test_master_console_ui.py tests/test_master_console_contracts.py tests/test_habits_controller_status.py tests/test_runtime_cache_and_shared_payload.py tests/test_slot_service_commands.py
```

Result:

- superseded by the full regression below

Full regression command:

```bash
env QT_QPA_PLATFORM=offscreen .venv-master-console/bin/python -m pytest -q tests
```

Latest full regression command:

```bash
env QT_QPA_PLATFORM=offscreen .venv-pyqt-debug/bin/python -m pytest tests -q
```

Result:

- `304 passed in 3.62s`

Latest red/green checks added in this pass:

- runtime JSON command-poll and history-defer keys are now declared in
  `DEFAULT_RUNTIME_CONFIG` and loaded by `load_runtime_config()`;
- fixed-size and variable-size EDF filler interpolation now inserts missing
  packet filler between the penultimate and final received packet;
- detail attach/detach commands preempt neural/Habits/RF connect commands in
  the SlotService queue;
- shutdown force-flushes deferred history while sampling.
- Windows save queue payloads use file-backed transport instead of expiring
  shared-memory names, preventing `OpenFileMapping` / `WinError 2` in the save
  process.
- Local detail chart throttling is intentionally low-peak: runtime config now
  sends `detail_publish_min_interval_ms` to the neural reader, drains at most
  one local chart frame per timer tick, uses a 16 ms render timer, and applies
  chart-only backpressure for slow render/read-gap pressure.
- Mode3 detail chart payloads are now capped on the display path only:
  LFP/ESA keeps the latest 80 packets per GUI emit and raw keeps the latest 60
  packets per GUI emit. The EDF/save path and packet-loss counters still see
  the full stream.
- Mode3 stream interval normalization clamps legacy or learned oversized
  values to 80 packets for LFP/ESA and 60 packets for raw, preventing the
  dynamic controller from turning chart updates into very large low-frequency
  bursts.
- Local detail chart rendering now uses round-robin stream scheduling instead
  of always drawing LFP before raw when both streams are pending.
- Detail chart render telemetry now records per-stream render diagnostics
  (`lfp_buffer_update_ms`, `lfp_setdata_ms`, `esa_setdata_ms`,
  `raw_buffer_update_ms`, `raw_setdata_ms`, `raster_setdata_ms`) and includes
  the latest values in packet-gap system-log diagnostics.

## Hardware Validation Checklist

- Start Master Console with three cage profiles and confirm all three
  `SlotService` processes stay independent.
- Connect one neural device and verify serial open/close/reopen, sampling
  start/stop, and mode transitions.
- In mode 0, verify LFP chart updates, sensor/battery values, packet index
  tracking, packet loss display, and save progress.
- In mode 1, verify threshold sample collection, channel switching, spike chart
  detail payloads, and mode1 save output.
- In mode 2, verify selected channel display, packet loss tracking, and mode2
  save output.
- In mode 3, verify LFP/ESA/raster chart updates, mode3 raw channel display,
  reref mode status, trial alignment behavior, and mode3 save output.
- Force detail chart attach/detach and confirm backpressure/throttle status is
  visible without freezing Master Console.
- Leave detail chart open in each mode and confirm the per-mode
  `gui_update_intervals_by_mode` values adapt every 10 seconds and persist into
  the cage JSON.
- Confirm the Master Console read metrics for M0-M3 update only from neural
  serial `read_all()` byte sizes.
- Run an SD download while Habits is active and confirm serial messages remain
  responsive while files are written by the background queue.
- Run with global save enabled and writer intentionally slowed or stressed;
  confirm `writer_lag` and save enqueue telemetry are visible. Since the EDF
  queue is unbounded/blocking now, watch for memory growth or slow enqueue
  rather than intentional dropped chunks.
- Stress serial input long enough to observe `pipeline.raw_queue_depth`,
  `processed_frames`, `dropped_raw_frames`, serial backlog telemetry, and UI
  responsiveness.
- Unplug/replug the neural serial device during idle and during sampling;
  confirm disconnect status, recovery behavior, and no orphan pipeline worker.
- Perform a multi-hour Windows soak with camera recording, RF/Habits connected,
  detail chart open, and save enabled for at least one high-rate mode.

## Parity Risks Found

- Pipeline overload no longer drops oldest raw frames. If processing remains
  overloaded past the bounded enqueue wait, new raw frames can still be rejected
  and reported through `dropped_raw_frames`; this must be validated against
  packet loss/status expectations on real high-rate hardware.
- `_save_put()` is now blocking on an unbounded queue. This avoids intentional
  EDF chunk loss, but real hardware soak tests should watch RAM and enqueue
  latency if disk writing falls behind for long periods.
- Dynamic GUI intervals optimize primarily for packet loss. The starting values
  and adaptation step should be validated on real hardware, especially in mode3
  where chart load previously correlated with loss growth.
- Legacy decode/save/gui code now runs on the pipeline worker thread instead of
  directly in the serial read loop. The core order is preserved, but long-run
  validation should watch for timing-sensitive assumptions in GUI buffer
  handoff and Qt signal delivery.
- Pipeline events are drained by the serial read loop. Normal frame processing
  happens in the worker callback, but final status/error events near shutdown
  should be checked on hardware to ensure they are visible before close.
- File-backed packed save messages need Windows soak testing to confirm temp
  payload files are consumed and removed after writer failures, process
  shutdown, or queue decode errors.
