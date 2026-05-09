# Final EDF To RHD2132 To Blue-Box Mapping

Generated on `2026-04-09`.

## Summary

- This is the final combined reference for:
  - `EDF label/channel`
  - `RHD2132 sampled channel`
  - `blue-box physical channel`
- Sampled `RHD2132` channels are:
  - `8, 9, 10, 11, 12, 13, 14, 15, 16, 17, 18, 19, 20, 21, 22, 23`
- Sampled blue-box channels are:
  - `1, 2, 3, 4, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16, 17`
- Blue-box channel `5` is `REF`, not one of the 16 sampled neural channels.
- There are two EDF numbering baselines:
  - `mode0 lfp.edf`
  - `mode3 family`

## Derivation

- Firmware samples `RHD2132` convert channels `8..23`.
- The user-provided schematic shows:
  - `RHD in0..in15 -> IN0..IN15`
- You then provided the corrected final correspondence for:
  - `RHD8..23 -> blue-box channel number`
- That user-confirmed corrected list overrides the earlier image-derived estimate.
- Therefore the final chain is:

`EDF label -> RHD2132 channel -> blue-box channel`

## Table A: mode0 `lfp.edf`

This table applies only to `mode0 lfp.edf`.

| EDF label | EDF index | RHD2132 channel | Blue-box channel |
|---|---:|---:|---:|
| Ch0 | 0 | 8 | 7 |
| Ch1 | 1 | 9 | 9 |
| Ch2 | 2 | 10 | 6 |
| Ch3 | 3 | 11 | 8 |
| Ch4 | 4 | 12 | 10 |
| Ch5 | 5 | 13 | 4 |
| Ch6 | 6 | 14 | 11 |
| Ch7 | 7 | 15 | 12 |
| Ch8 | 8 | 16 | 13 |
| Ch9 | 9 | 17 | 14 |
| Ch10 | 10 | 18 | 15 |
| Ch11 | 11 | 19 | 17 |
| Ch12 | 12 | 20 | 1 |
| Ch13 | 13 | 21 | 3 |
| Ch14 | 14 | 22 | 16 |
| Ch15 | 15 | 23 | 2 |

## Table B: mode3 Family

This one shared table applies to:

- `mode3 LFP&ESA.edf` `Ch0..Ch15`
- `mode3 LFP&ESA.edf` `ESA0..ESA15`
- `mode3 LFP&ESA.edf` `Raster0..Raster15`
- `mode3_raw.edf` `RawChannel = N`
- `mode2 AP_LFP_Raw_data.edf` `Ch0..Ch15`
- `mode1` GUI channel numbering
- `mode1.edf` spike annotations `Spike@ChN`

Important:

- `ChN`, `ESAN`, and `RasterN` always refer to the same physical electrode in mode3.
- `lfp0 = esa0 = raster0 = RawChannel 0 = mode1 GUI channel 0`

| Shared index N | mode3 LFP | mode3 ESA | mode3 Raster | mode3 RawChannel | mode1 GUI channel | mode1 annotation | mode2 label | RHD2132 channel | Blue-box channel |
|---|---|---|---|---:|---:|---|---|---:|---:|
| 0 | Ch0 | ESA0 | Raster0 | 0 | 0 | Spike@Ch0 | Ch0 | 22 | 16 |
| 1 | Ch1 | ESA1 | Raster1 | 1 | 1 | Spike@Ch1 | Ch1 | 23 | 2 |
| 2 | Ch2 | ESA2 | Raster2 | 2 | 2 | Spike@Ch2 | Ch2 | 8 | 7 |
| 3 | Ch3 | ESA3 | Raster3 | 3 | 3 | Spike@Ch3 | Ch3 | 9 | 9 |
| 4 | Ch4 | ESA4 | Raster4 | 4 | 4 | Spike@Ch4 | Ch4 | 10 | 6 |
| 5 | Ch5 | ESA5 | Raster5 | 5 | 5 | Spike@Ch5 | Ch5 | 11 | 8 |
| 6 | Ch6 | ESA6 | Raster6 | 6 | 6 | Spike@Ch6 | Ch6 | 12 | 10 |
| 7 | Ch7 | ESA7 | Raster7 | 7 | 7 | Spike@Ch7 | Ch7 | 13 | 4 |
| 8 | Ch8 | ESA8 | Raster8 | 8 | 8 | Spike@Ch8 | Ch8 | 14 | 11 |
| 9 | Ch9 | ESA9 | Raster9 | 9 | 9 | Spike@Ch9 | Ch9 | 15 | 12 |
| 10 | Ch10 | ESA10 | Raster10 | 10 | 10 | Spike@Ch10 | Ch10 | 16 | 13 |
| 11 | Ch11 | ESA11 | Raster11 | 11 | 11 | Spike@Ch11 | Ch11 | 17 | 14 |
| 12 | Ch12 | ESA12 | Raster12 | 12 | 12 | Spike@Ch12 | Ch12 | 18 | 15 |
| 13 | Ch13 | ESA13 | Raster13 | 13 | 13 | Spike@Ch13 | Ch13 | 19 | 17 |
| 14 | Ch14 | ESA14 | Raster14 | 14 | 14 | Spike@Ch14 | Ch14 | 20 | 1 |
| 15 | Ch15 | ESA15 | Raster15 | 15 | 15 | Spike@Ch15 | Ch15 | 21 | 3 |

## Table C: `mode1.edf` Raw Signal

`mode1.edf` contains only one waveform label:

- `Raw`

This `Raw` label is not self-identifying in the EDF header. It must be combined with the selected mode1 GUI channel. That selected GUI channel follows the same mapping as Table B.

| mode1 selected GUI channel | `mode1.edf` Raw -> RHD2132 channel | Blue-box channel |
|---:|---:|---:|
| 0 | 22 | 16 |
| 1 | 23 | 2 |
| 2 | 8 | 7 |
| 3 | 9 | 9 |
| 4 | 10 | 6 |
| 5 | 11 | 8 |
| 6 | 12 | 10 |
| 7 | 13 | 4 |
| 8 | 14 | 11 |
| 9 | 15 | 12 |
| 10 | 16 | 13 |
| 11 | 17 | 14 |
| 12 | 18 | 15 |
| 13 | 19 | 17 |
| 14 | 20 | 1 |
| 15 | 21 | 3 |

## Non-Neural EDF Signals

These EDF signals do not map to a neural electrode:

| EDF context | EDF label | RHD2132 channel | Blue-box channel |
|---|---|---|---|
| sensor.edf | all sensor channels | none | none |
| mode3_raw.edf | Alignment | none | none |

## Blue-Box Reference

| Blue-box channel | Net | Sampled in RHD2132 8..23 |
|---:|---|---|
| 5 | REF | No |

## Final Conclusion

- `mode0 lfp.edf` uses one ordering.
- The `mode3 family` uses another ordering.
- This file is the final consolidated table for:

`EDF -> RHD2132 -> blue-box channel`

## 16-Site Linear Silicon Probe

You provided one silicon electrode with 16 sites arranged as a single vertical column.

- site order: bottom to top
- site sequence: `site0 .. site15`
- corresponding blue-box channels from bottom to top:
  - `8, 1, 7, 2, 9, 16, 6, 3, 10, 17, 4, 15, 11, 14, 12, 13`

### Silicon Probe Final Mapping

| Site | Blue-box channel | RHD2132 channel | mode0 EDF | mode3 family |
|---:|---:|---:|---|---|
| 0 | 8 | 11 | Ch3 | Ch5 / ESA5 / Raster5 |
| 1 | 1 | 20 | Ch12 | Ch14 / ESA14 / Raster14 |
| 2 | 7 | 8 | Ch0 | Ch2 / ESA2 / Raster2 |
| 3 | 2 | 23 | Ch15 | Ch1 / ESA1 / Raster1 |
| 4 | 9 | 9 | Ch1 | Ch3 / ESA3 / Raster3 |
| 5 | 16 | 22 | Ch14 | Ch0 / ESA0 / Raster0 |
| 6 | 6 | 10 | Ch2 | Ch4 / ESA4 / Raster4 |
| 7 | 3 | 21 | Ch13 | Ch15 / ESA15 / Raster15 |
| 8 | 10 | 12 | Ch4 | Ch6 / ESA6 / Raster6 |
| 9 | 17 | 19 | Ch11 | Ch13 / ESA13 / Raster13 |
| 10 | 4 | 13 | Ch5 | Ch7 / ESA7 / Raster7 |
| 11 | 15 | 18 | Ch10 | Ch12 / ESA12 / Raster12 |
| 12 | 11 | 14 | Ch6 | Ch8 / ESA8 / Raster8 |
| 13 | 14 | 17 | Ch9 | Ch11 / ESA11 / Raster11 |
| 14 | 12 | 15 | Ch7 | Ch9 / ESA9 / Raster9 |
| 15 | 13 | 16 | Ch8 | Ch10 / ESA10 / Raster10 |

### SVG Figure

An SVG figure showing the same site-to-EDF mapping for two silicon electrodes has been created here:

- [silicon_probe_site_to_edf.svg](/Users/yubowen/Desktop/博士课题/Wireless_24:7Recording-SyncFold/neural_recorder_GUI/silicon_probe_site_to_edf.svg)

In the SVG:

- the left probe shows `mode0 lfp.edf`
- the right probe shows the `mode3 family`
