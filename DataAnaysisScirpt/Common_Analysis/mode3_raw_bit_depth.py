#!/usr/bin/env python3
"""
Analyze Mode3 RawData EDF bit-plane usage and reduced bit-depth error.

The recorder saves Mode3 raw samples as physical uV values in an EDF channel
named RawData. Those values were originally decoded from unsigned 16-bit RHD
words by the same linear mapping used in neural_reader.py, so this script
reconstructs the RHD code before computing bit statistics.
"""

from __future__ import annotations

import argparse
import csv
import json
import math
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple

import numpy as np

try:
    import pyedflib
except Exception as exc:  # pragma: no cover - exercised by CLI users
    pyedflib = None
    _PYEDFLIB_IMPORT_ERROR = exc
else:
    _PYEDFLIB_IMPORT_ERROR = None


RHD_VREF = 1.225
RHD_GAIN = 192.0
RHD_DAC_RESOLUTION = (RHD_VREF * 2.0) / 65535.0
RHD_UV_PER_CODE = RHD_DAC_RESOLUTION / RHD_GAIN * 1_000_000.0


def reconstruct_rhd_code(raw_uv: np.ndarray) -> np.ndarray:
    """Invert the recorder's RHD unsigned-code-to-uV mapping."""
    raw_uv = np.asarray(raw_uv, dtype=np.float64)
    codes = np.rint(((raw_uv / 1_000_000.0) * RHD_GAIN + RHD_VREF) / RHD_DAC_RESOLUTION)
    return np.clip(codes, 0, 65535).astype(np.uint16)


def codes_to_uv(codes: np.ndarray) -> np.ndarray:
    codes = np.asarray(codes, dtype=np.float64)
    return ((codes * RHD_DAC_RESOLUTION) - RHD_VREF) / RHD_GAIN * 1_000_000.0


def normalized_label(label: str) -> str:
    return "".join(ch for ch in str(label).lower() if ch.isalnum())


def find_signal(labels: List[str], candidates: Iterable[str]) -> Optional[int]:
    normalized_candidates = {normalized_label(item) for item in candidates}
    normalized_labels = [normalized_label(item) for item in labels]
    for idx, label in enumerate(normalized_labels):
        if label in normalized_candidates:
            return idx
    for idx, label in enumerate(normalized_labels):
        if any(candidate in label for candidate in normalized_candidates):
            return idx
    return None


def shannon_entropy_from_counts(counts: np.ndarray) -> float:
    total = int(np.sum(counts))
    if total <= 0:
        return 0.0
    probs = counts[counts > 0].astype(np.float64) / float(total)
    return float(-np.sum(probs * np.log2(probs)))


def bit_rows_from_hist(hist: np.ndarray, total: int, prefix: Dict[str, object]) -> List[Dict[str, object]]:
    values = np.arange(65536, dtype=np.uint32)
    rows = []
    if total <= 0:
        return rows
    for bit in range(16):
        mask = ((values >> bit) & 1).astype(bool)
        ones = int(np.sum(hist[mask]))
        zeros = int(total - ones)
        p_one = float(ones / total)
        entropy = 0.0
        if 0.0 < p_one < 1.0:
            entropy = float(-(p_one * math.log2(p_one) + (1.0 - p_one) * math.log2(1.0 - p_one)))
        row = dict(prefix)
        row.update(
            {
                "bit": bit,
                "zeros": zeros,
                "ones": ones,
                "p_one": p_one,
                "bit_entropy": entropy,
            }
        )
        rows.append(row)
    return rows


def quantization_rows_from_hist(
    hist: np.ndarray,
    min_code: int,
    max_code: int,
    signal_std_uv: float,
    prefix: Dict[str, object],
    min_bits: int,
) -> List[Dict[str, object]]:
    values = np.arange(65536, dtype=np.int64)
    counts = hist.astype(np.float64)
    total = float(np.sum(counts))
    rows: List[Dict[str, object]] = []
    if total <= 0:
        return rows

    used_mask = hist > 0
    used_values = values[used_mask]
    used_counts = counts[used_mask]

    for bits in range(16, min_bits - 1, -1):
        shift = 16 - bits
        if shift <= 0:
            fixed_recon = used_values
        else:
            step = 1 << shift
            fixed_recon = ((used_values + step // 2) // step) * step
            fixed_recon = np.clip(fixed_recon, 0, 65535)
        fixed_err_uv = (fixed_recon - used_values).astype(np.float64) * RHD_UV_PER_CODE

        if max_code <= min_code or bits >= 16:
            minmax_recon = used_values
        else:
            levels = (1 << bits) - 1
            q = np.rint((used_values - min_code) * levels / float(max_code - min_code))
            minmax_recon = np.rint(q * float(max_code - min_code) / levels + min_code)
            minmax_recon = np.clip(minmax_recon, 0, 65535)
        minmax_err_uv = (minmax_recon - used_values).astype(np.float64) * RHD_UV_PER_CODE

        for method, err_uv in (
            ("drop_lsb_global_16bit", fixed_err_uv),
            ("minmax_observed_range", minmax_err_uv),
        ):
            abs_err = np.abs(err_uv)
            rms = float(math.sqrt(np.sum(used_counts * err_uv * err_uv) / total))
            mean_abs = float(np.sum(used_counts * abs_err) / total)
            max_abs = float(np.max(abs_err)) if abs_err.size else 0.0
            if signal_std_uv > 0 and rms > 0:
                snr_db = float(20.0 * math.log10(signal_std_uv / rms))
                err_pct_std = float(100.0 * rms / signal_std_uv)
            elif rms == 0:
                snr_db = math.inf
                err_pct_std = 0.0
            else:
                snr_db = math.nan
                err_pct_std = math.nan
            row = dict(prefix)
            row.update(
                {
                    "bits": bits,
                    "method": method,
                    "rms_error_uv": rms,
                    "mean_abs_error_uv": mean_abs,
                    "max_abs_error_uv": max_abs,
                    "snr_db_vs_signal_std": snr_db,
                    "rms_error_percent_of_signal_std": err_pct_std,
                }
            )
            rows.append(row)
    return rows


def weighted_code_stats(hist: np.ndarray) -> Tuple[int, int, float, float, int, float]:
    total = int(np.sum(hist))
    if total <= 0:
        return 0, 0, math.nan, math.nan, 0, 0.0
    values = np.arange(65536, dtype=np.float64)
    used = hist > 0
    min_code = int(np.argmax(used))
    max_code = int(len(used) - 1 - np.argmax(used[::-1]))
    mean_code = float(np.sum(values * hist) / total)
    var_code = float(np.sum(hist * (values - mean_code) ** 2) / total)
    unique_codes = int(np.count_nonzero(hist))
    entropy = shannon_entropy_from_counts(hist)
    return min_code, max_code, mean_code, math.sqrt(max(0.0, var_code)), unique_codes, entropy


def percentile_from_hist(hist: np.ndarray, percentiles: Iterable[float]) -> Dict[str, int]:
    total = int(np.sum(hist))
    if total <= 0:
        return {f"p{p:g}_code": 0 for p in percentiles}
    cdf = np.cumsum(hist)
    out = {}
    for p in percentiles:
        target = (float(p) / 100.0) * max(0, total - 1)
        out[f"p{p:g}_code"] = int(np.searchsorted(cdf, target + 1, side="left"))
    return out


def analyze_edf(
    edf_path: Path,
    raw_label: str = "RawData",
    raw_channel_label: str = "RawChannel",
    only_raw_channel: Optional[int] = None,
    chunk_samples: int = 2_000_000,
    max_samples: Optional[int] = None,
    start_sample: int = 0,
    missing_threshold_uv: float = -999.5,
    min_bits: int = 8,
) -> Dict[str, object]:
    if pyedflib is None:
        raise RuntimeError(f"pyedflib is not available: {_PYEDFLIB_IMPORT_ERROR!r}")

    reader = pyedflib.EdfReader(str(edf_path))
    try:
        labels = list(reader.getSignalLabels())
        raw_idx = find_signal(labels, [raw_label, "RawData", "Raw Data"])
        if raw_idx is None:
            raise ValueError(f"Could not find RawData channel. EDF labels: {labels}")
        channel_idx = find_signal(labels, [raw_channel_label, "RawChannel", "Raw Channel"])

        sample_rate = float(reader.getSampleFrequency(raw_idx))
        total_samples = int(reader.getNSamples()[raw_idx])
        start = max(0, int(start_sample))
        if start >= total_samples:
            raise ValueError(f"start_sample {start} is outside the EDF signal length {total_samples}")
        read_limit = total_samples - start
        if max_samples is not None and int(max_samples) > 0:
            read_limit = min(read_limit, int(max_samples))

        hist = np.zeros(65536, dtype=np.int64)
        hist_by_channel = np.zeros((16, 65536), dtype=np.int64)
        transition_ones = np.zeros(16, dtype=np.int64)
        transition_total = 0
        previous_code: Optional[int] = None
        clipped_low = 0
        clipped_high = 0
        missing_or_invalid = 0
        samples_seen = 0
        channel_counts = np.zeros(16, dtype=np.int64)

        cursor = start
        remaining = read_limit
        while remaining > 0:
            n = int(min(chunk_samples, remaining))
            raw_uv = np.asarray(reader.readSignal(raw_idx, start=cursor, n=n), dtype=np.float64)
            valid = np.isfinite(raw_uv)
            if channel_idx is not None:
                raw_ch = np.asarray(reader.readSignal(channel_idx, start=cursor, n=n), dtype=np.float64)
                raw_ch_int = np.rint(raw_ch).astype(np.int64)
                valid &= (raw_ch_int >= 0) & (raw_ch_int < 16)
                if only_raw_channel is not None:
                    valid &= raw_ch_int == int(only_raw_channel)
            else:
                raw_ch_int = np.full(raw_uv.shape, -1, dtype=np.int64)
                valid &= raw_uv > float(missing_threshold_uv)

            missing_or_invalid += int(raw_uv.size - np.count_nonzero(valid))
            if np.any(valid):
                valid_uv = raw_uv[valid]
                clipped_low += int(np.count_nonzero(valid_uv <= -999.95))
                clipped_high += int(np.count_nonzero(valid_uv >= 999.95))
                codes = reconstruct_rhd_code(valid_uv)
                code_i64 = codes.astype(np.int64)
                hist += np.bincount(code_i64, minlength=65536)

                if channel_idx is not None:
                    valid_ch = raw_ch_int[valid]
                    for ch in range(16):
                        ch_mask = valid_ch == ch
                        if np.any(ch_mask):
                            channel_counts[ch] += int(np.count_nonzero(ch_mask))
                            hist_by_channel[ch] += np.bincount(code_i64[ch_mask], minlength=65536)

                codes_for_transitions = code_i64
                if previous_code is not None and codes_for_transitions.size > 0:
                    xor_first = int(previous_code) ^ int(codes_for_transitions[0])
                    for bit in range(16):
                        transition_ones[bit] += (xor_first >> bit) & 1
                    transition_total += 1
                if codes_for_transitions.size > 1:
                    xors = np.bitwise_xor(codes_for_transitions[:-1], codes_for_transitions[1:])
                    for bit in range(16):
                        transition_ones[bit] += int(np.count_nonzero((xors >> bit) & 1))
                    transition_total += int(codes_for_transitions.size - 1)
                previous_code = int(codes_for_transitions[-1])

            samples_seen += int(raw_uv.size)
            cursor += n
            remaining -= n
    finally:
        reader.close()

    valid_samples = int(np.sum(hist))
    min_code, max_code, mean_code, std_code, unique_codes, entropy = weighted_code_stats(hist)
    signal_std_uv = float(std_code * RHD_UV_PER_CODE) if valid_samples > 0 else math.nan
    prefix = {"scope": "overall", "channel": "all"}

    bit_rows = bit_rows_from_hist(hist, valid_samples, prefix)
    for row in bit_rows:
        bit = int(row["bit"])
        row["transition_fraction"] = (
            float(transition_ones[bit] / transition_total) if transition_total > 0 else math.nan
        )

    channel_bit_rows = []
    for ch in range(16):
        ch_total = int(channel_counts[ch])
        if ch_total <= 0:
            continue
        channel_bit_rows.extend(
            bit_rows_from_hist(hist_by_channel[ch], ch_total, {"scope": "raw_channel", "channel": ch})
        )

    quant_rows = quantization_rows_from_hist(
        hist=hist,
        min_code=min_code,
        max_code=max_code,
        signal_std_uv=signal_std_uv,
        prefix=prefix,
        min_bits=min_bits,
    )

    summary = {
        "file": str(edf_path),
        "labels": labels,
        "raw_label": labels[raw_idx],
        "raw_channel_label": labels[channel_idx] if channel_idx is not None else None,
        "only_raw_channel": only_raw_channel,
        "sample_rate_hz": sample_rate,
        "total_samples_in_signal": total_samples,
        "samples_read": samples_seen,
        "valid_samples": valid_samples,
        "missing_or_invalid_samples": missing_or_invalid,
        "valid_fraction_of_read": float(valid_samples / samples_seen) if samples_seen else 0.0,
        "rhd_uv_per_code": RHD_UV_PER_CODE,
        "min_code": min_code,
        "max_code": max_code,
        "code_range": int(max_code - min_code),
        "lossless_offset_bits_for_observed_range": int(
            math.ceil(math.log2(max(1, max_code - min_code + 1)))
        ),
        "mean_code": mean_code,
        "std_code": std_code,
        "signal_std_uv": signal_std_uv,
        "unique_codes": unique_codes,
        "shannon_entropy_bits_per_sample": entropy,
        "delta_transition_samples": transition_total,
        "clipped_or_at_low_physical_limit_samples": clipped_low,
        "clipped_or_at_high_physical_limit_samples": clipped_high,
        "channel_counts": {str(ch): int(channel_counts[ch]) for ch in range(16) if channel_counts[ch] > 0},
    }
    summary.update(percentile_from_hist(hist, [0.1, 1, 5, 50, 95, 99, 99.9]))

    return {
        "summary": summary,
        "bit_rows": bit_rows,
        "channel_bit_rows": channel_bit_rows,
        "quantization_rows": quant_rows,
        "histogram": hist,
    }


def write_csv(path: Path, rows: List[Dict[str, object]]) -> None:
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    fieldnames = list(rows[0].keys())
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def write_outputs(result: Dict[str, object], output_dir: Path, write_histogram: bool = False) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    summary_path = output_dir / "mode3_raw_bit_depth_summary.json"
    bit_path = output_dir / "mode3_raw_bit_distribution.csv"
    channel_bit_path = output_dir / "mode3_raw_bit_distribution_by_channel.csv"
    quant_path = output_dir / "mode3_raw_quantization_error.csv"

    summary_path.write_text(
        json.dumps(result["summary"], ensure_ascii=False, indent=2, allow_nan=True),
        encoding="utf-8",
    )
    write_csv(bit_path, result["bit_rows"])
    write_csv(channel_bit_path, result["channel_bit_rows"])
    write_csv(quant_path, result["quantization_rows"])

    if write_histogram:
        hist = np.asarray(result["histogram"], dtype=np.int64)
        rows = [
            {"code": int(code), "count": int(count)}
            for code, count in enumerate(hist)
            if int(count) > 0
        ]
        write_csv(output_dir / "mode3_raw_code_histogram.csv", rows)


def print_human_summary(result: Dict[str, object], output_dir: Optional[Path]) -> None:
    summary = result["summary"]
    print(f"File: {summary['file']}")
    print(f"Raw channel: {summary['raw_label']} @ {summary['sample_rate_hz']:.3f} Hz")
    print(f"Read samples: {summary['samples_read']:,}; valid: {summary['valid_samples']:,} "
          f"({summary['valid_fraction_of_read']:.2%})")
    print(f"RHD code range: {summary['min_code']}..{summary['max_code']} "
          f"(range {summary['code_range']}, lossless offset bits {summary['lossless_offset_bits_for_observed_range']})")
    print(f"Signal std: {summary['signal_std_uv']:.3f} uV; entropy: "
          f"{summary['shannon_entropy_bits_per_sample']:.3f} bits/sample; unique codes: {summary['unique_codes']}")

    print("\nBit distribution, overall:")
    print("bit  p_one    transition")
    for row in result["bit_rows"]:
        print(f"{int(row['bit']):>2}   {float(row['p_one']):>6.3f}   {float(row['transition_fraction']):>6.3f}")

    print("\nFixed 16-bit LSB-drop quantization:")
    print("bits  rms_uV  max_uV  err/std  snr_dB")
    for row in result["quantization_rows"]:
        if row["method"] != "drop_lsb_global_16bit":
            continue
        bits = int(row["bits"])
        rms = float(row["rms_error_uv"])
        max_abs = float(row["max_abs_error_uv"])
        err_pct = float(row["rms_error_percent_of_signal_std"])
        snr = float(row["snr_db_vs_signal_std"])
        print(f"{bits:>4}  {rms:>6.3f}  {max_abs:>6.3f}  {err_pct:>6.2f}%  {snr:>6.2f}")

    if output_dir is not None:
        print(f"\nWrote: {output_dir}")


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Analyze RHD 16-bit bit usage in a Mode3 raw EDF file."
    )
    parser.add_argument("edf", type=Path, help="Path to a *mode3_raw.edf or raw_data.edf file.")
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=None,
        help="Directory for JSON/CSV outputs. Defaults to <edf stem>_bit_depth next to the EDF.",
    )
    parser.add_argument("--raw-label", default="RawData", help="EDF label for the raw uV signal.")
    parser.add_argument("--raw-channel-label", default="RawChannel", help="EDF label for raw channel index.")
    parser.add_argument("--only-raw-channel", type=int, default=None, help="Analyze only one RawChannel index.")
    parser.add_argument("--chunk-samples", type=int, default=2_000_000, help="Chunk size for streaming reads.")
    parser.add_argument("--max-samples", type=int, default=None, help="Optional cap for quick sampling.")
    parser.add_argument("--start-sample", type=int, default=0, help="Start sample offset.")
    parser.add_argument("--missing-threshold-uv", type=float, default=-999.5)
    parser.add_argument("--min-bits", type=int, default=8, help="Smallest reduced bit depth to evaluate.")
    parser.add_argument("--write-histogram", action="store_true", help="Also write non-zero code histogram CSV.")
    return parser


def main(argv: Optional[List[str]] = None) -> int:
    parser = build_arg_parser()
    args = parser.parse_args(argv)
    edf_path = args.edf.expanduser().resolve()
    if not edf_path.exists():
        parser.error(f"EDF file does not exist: {edf_path}")

    output_dir = args.output_dir
    if output_dir is None:
        output_dir = edf_path.with_name(f"{edf_path.stem}_bit_depth")
    output_dir = output_dir.expanduser().resolve()

    result = analyze_edf(
        edf_path=edf_path,
        raw_label=args.raw_label,
        raw_channel_label=args.raw_channel_label,
        only_raw_channel=args.only_raw_channel,
        chunk_samples=args.chunk_samples,
        max_samples=args.max_samples,
        start_sample=args.start_sample,
        missing_threshold_uv=args.missing_threshold_uv,
        min_bits=max(1, min(16, int(args.min_bits))),
    )
    write_outputs(result, output_dir, write_histogram=bool(args.write_histogram))
    print_human_summary(result, output_dir)
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
