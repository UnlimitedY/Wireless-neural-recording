# Neural Recorder GUI Parity Matrix

## Scope
This matrix tracks parity between the legacy single-window GUI and the current
master-console plus detail-view architecture.

## Implemented And Behavior-Aligned

| Area | Legacy behavior | Current status |
| --- | --- | --- |
| Master console core control | Neural/RF/Habits/Camera connect, save, RF power, relay, IMU, reref | Implemented in master console cards |
| Detail chart data flow | Per-cage independent detail window with high-frequency plot stream | Implemented |
| Save/link behavior | Save start/stop, video follow-save, daily save paths | Implemented |
| Charging guard | 10 s polling, hit accumulation, mouse detection state, config dialog | Implemented |
| Habits status/cache | Trial/event/state cache restore and live updates | Implemented |
| Auto threshold | Mode1 16-channel sweep using legacy window length and raw-index threshold storage | Implemented |
| Impedance history | Per-cage cache restore and refresh in detail chart | Implemented |

## Implemented But Not Identical By Design

| Area | Legacy behavior | Current status |
| --- | --- | --- |
| Spectrum windows | Opened from the old single GUI | Kept in detail chart only, not duplicated into master console |
| Spike filter controls | Lived only in the old single chart window | Shared config now exists, but the live filter effect still belongs to the detail chart |
| Impedance compensation | Local impedance panel parameters | Shared between slot status and detail chart, master console exposes config without duplicating the chart |

## Still Visible But Intentionally Detail-Local

| Control | Reason |
| --- | --- |
| `Open Spectrum` (Single Channel Spike) | Pure chart analysis window, no hardware/backend side effect |
| `LFP Spectrum Analysis` | Pure chart analysis window, no hardware/backend side effect |
| LFP local filter buttons | Pure plot presentation behavior |
| Impedance display/history mode toggles | Pure detail chart presentation behavior |

## Shared State Added For Parity

The following states now exist in slot status so master/detail stay aligned:

- `neural.spike_filter_enabled`
- `neural.spike_filter_low_cut_hz`
- `neural.spike_filter_high_cut_hz`
- `neural.spike_filter_sample_rate_hz`
- `neural.rc_series_resistor_kohm`
- `neural.rc_shunt_cap_pf`
- `neural.ui_capabilities`
- `neural.ui_busy`
- `neural.ui_pending`
- `neural.telemetry`

## Remaining Watch Items

- Review old GUI-only debug helpers and decide whether they should stay in `may_useful` or get formal parity support.
- Continue trimming silent `except` branches where a real user-visible failure should produce `System Log`.
- Validate long-run Windows performance with 3 cages plus chart windows plus camera recording on real hardware.
