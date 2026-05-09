import math
import os
from pathlib import Path

import numpy as np

from .behavior_analysis import (
    BIAS_STATE_LABELS,
    PERF_STATE_LABELS,
    classify_bias_state,
    classify_perf_state,
    epoch_ms_to_iso,
)
from .trial_parsing import TeventData, TrialData


SAMPLE_START_EVENT_ID = 0
TRIAL_END_EVENT_ID = 1
LEFT_LICK_EVENT_ID = 8
RIGHT_LICK_EVENT_ID = 10
CHOICE_EVENT_IDS = {LEFT_LICK_EVENT_ID, RIGHT_LICK_EVENT_ID}

RT_CLASS_LABELS = {
    -1: "unknown",
    0: "low",
    1: "middle",
    2: "high",
}

DEFAULT_RT_CONFIG = {
    "min_rt_ms": 50.0,
    "max_rt_ms": 10000.0,
    "n_components": 3,
    "max_iter": 200,
    "tol": 1e-6,
    "random_seed": 0,
}


def parse_rt_trials(sd_card_dir, slice_start_ms=None, slice_end_ms=None, config=None):
    cfg = dict(DEFAULT_RT_CONFIG)
    if config:
        cfg.update(config)
    records = []
    warnings = []
    for trial_file in sorted(Path(sd_card_dir).rglob("Trial.txt")):
        tevent_file = trial_file.with_name("Tevent.txt")
        tevents, tevent_warnings = _parse_tevent_file(tevent_file)
        warnings.extend(tevent_warnings)
        with open(trial_file, "r", encoding="utf-8", errors="replace") as handle:
            for line_no, line in enumerate(handle, start=1):
                if "Trial:" in line or "Tevent:" in line or not line.strip():
                    continue
                try:
                    trial = TrialData(line)
                    trial_epoch_ms = int(round(float(trial.time) * 1000.0))
                except Exception as exc:
                    warnings.append(f"Failed to parse {trial_file}:{line_no}: {exc}")
                    continue
                if slice_start_ms is not None and trial_epoch_ms < float(slice_start_ms) - cfg["max_rt_ms"]:
                    continue
                if slice_end_ms is not None and trial_epoch_ms >= float(slice_end_ms) + cfg["max_rt_ms"]:
                    continue
                tevent = tevents.get(int(trial.trial_num))
                record, record_warnings = compute_trial_rt_record(
                    trial,
                    tevent,
                    trial_epoch_ms,
                    source_file=str(trial_file.resolve()),
                    line_no=line_no,
                    config=cfg,
                )
                warnings.extend(record_warnings)
                if slice_start_ms is not None and record["sample_start_epoch_ms"] is not None:
                    if float(record["sample_start_epoch_ms"]) < float(slice_start_ms):
                        continue
                if slice_end_ms is not None and record["sample_start_epoch_ms"] is not None:
                    if float(record["sample_start_epoch_ms"]) >= float(slice_end_ms):
                        continue
                records.append(record)
    records.sort(key=lambda item: (
        float("inf") if item.get("sample_start_epoch_ms") is None else float(item["sample_start_epoch_ms"]),
        item.get("trial_num", -1),
        item.get("source_file", ""),
    ))
    classified, fit = classify_rt_records(records, config=cfg)
    return classified, fit, warnings


def compute_trial_rt_record(trial, tevent, trial_epoch_ms, source_file="", line_no=0, config=None):
    cfg = dict(DEFAULT_RT_CONFIG)
    if config:
        cfg.update(config)
    warnings = []
    sample_event_ts = None
    choice_event_id = -1
    choice_event_ts = None
    if tevent is not None:
        for event_id, timestamp in sorted(tevent.events, key=lambda item: item[1]):
            if int(event_id) == SAMPLE_START_EVENT_ID:
                sample_event_ts = int(timestamp)
                break
        if sample_event_ts is not None:
            for event_id, timestamp in sorted(tevent.events, key=lambda item: item[1]):
                if int(event_id) in CHOICE_EVENT_IDS and int(timestamp) >= sample_event_ts:
                    choice_event_id = int(event_id)
                    choice_event_ts = int(timestamp)
                    break
    else:
        warnings.append(f"No Tevent found for {source_file}:{line_no} trial {trial.trial_num}")

    rt_ms = np.nan
    rt_valid = False
    if sample_event_ts is None:
        warnings.append(f"No sample-start event for {source_file}:{line_no} trial {trial.trial_num}")
    elif choice_event_ts is None:
        warnings.append(f"No post-sample choice lick for {source_file}:{line_no} trial {trial.trial_num}")
    else:
        rt_ms = float(choice_event_ts - sample_event_ts)
        rt_valid = bool(cfg["min_rt_ms"] <= rt_ms <= cfg["max_rt_ms"])
        if not rt_valid:
            warnings.append(f"Invalid RT {rt_ms:g} ms for {source_file}:{line_no} trial {trial.trial_num}")

    origin_ts = _trial_relative_origin_ts(trial)
    sample_start_epoch_ms = None
    choice_epoch_ms = None
    if sample_event_ts is not None:
        sample_start_epoch_ms = float(trial_epoch_ms) + float(sample_event_ts - origin_ts)
    if sample_start_epoch_ms is not None and np.isfinite(rt_ms):
        choice_epoch_ms = float(sample_start_epoch_ms) + float(rt_ms)

    return {
        "source_file": str(source_file),
        "line_no": int(line_no),
        "trial_num": int(trial.trial_num),
        "trial_epoch_ms": int(trial_epoch_ms),
        "trial_epoch_iso": epoch_ms_to_iso(trial_epoch_ms),
        "trial_type": int(trial.trial_type),
        "trial_outcome": int(trial.trial_outcome),
        "rule": int(trial.rule),
        "correct_flag": 1 if int(trial.trial_outcome) == 1 else 0 if int(trial.trial_outcome) == 2 else -1,
        "sample_event_ts_ms": sample_event_ts,
        "choice_event_id": int(choice_event_id),
        "choice_event_ts_ms": choice_event_ts,
        "sample_start_epoch_ms": sample_start_epoch_ms,
        "sample_start_iso": epoch_ms_to_iso(sample_start_epoch_ms) if sample_start_epoch_ms is not None else "",
        "choice_epoch_ms": choice_epoch_ms,
        "choice_iso": epoch_ms_to_iso(choice_epoch_ms) if choice_epoch_ms is not None else "",
        "rt_ms": float(rt_ms) if np.isfinite(rt_ms) else np.nan,
        "rt_valid": int(rt_valid),
        "rt_class": -1,
        "rt_class_label": RT_CLASS_LABELS[-1],
        "trial_start_state_ts_ms": int(getattr(trial, "start_ts", 0) or 0),
        "trial_start_timestamp_ms": (
            float(trial.trial_start_timestamp_ms)
            if getattr(trial, "trial_start_timestamp_ms", None) is not None
            else np.nan
        ),
    }, warnings


def classify_rt_records(records, config=None):
    cfg = dict(DEFAULT_RT_CONFIG)
    if config:
        cfg.update(config)
    rt_values = np.asarray([float(item.get("rt_ms", np.nan)) for item in records], dtype=np.float64)
    valid = np.isfinite(rt_values) & (rt_values > 0)
    valid &= rt_values >= float(cfg["min_rt_ms"])
    valid &= rt_values <= float(cfg["max_rt_ms"])
    if np.count_nonzero(valid) < int(cfg["n_components"]):
        fit = _empty_rt_fit("not_enough_valid_rt", cfg)
        return _assign_rt_classes(records, np.full(rt_values.shape, -1, dtype=np.int8), fit), fit

    log_rt = np.log(rt_values[valid])
    gmm = fit_gaussian_mixture_1d(
        log_rt,
        n_components=int(cfg["n_components"]),
        max_iter=int(cfg["max_iter"]),
        tol=float(cfg["tol"]),
        random_seed=int(cfg["random_seed"]),
    )
    thresholds_log = gaussian_mixture_thresholds(gmm["means"], gmm["sigmas"], gmm["weights"])
    thresholds_ms = np.exp(thresholds_log)
    classes = np.full(rt_values.shape, -1, dtype=np.int8)
    classes[valid] = np.searchsorted(thresholds_ms, rt_values[valid], side="right").astype(np.int8)
    fit = {
        "method": "log_rt_3_component_gaussian_mixture",
        "n_valid_rt": int(np.count_nonzero(valid)),
        "thresholds_ms": [float(v) for v in thresholds_ms],
        "thresholds_log": [float(v) for v in thresholds_log],
        "component_means_log": [float(v) for v in gmm["means"]],
        "component_sigmas_log": [float(v) for v in gmm["sigmas"]],
        "component_weights": [float(v) for v in gmm["weights"]],
        "component_means_ms": [float(math.exp(v)) for v in gmm["means"]],
        "converged": bool(gmm["converged"]),
        "n_iter": int(gmm["n_iter"]),
        "config": cfg,
    }
    return _assign_rt_classes(records, classes, fit), fit


def compute_hourly_rt_features(records, slice_start_ms, slice_end_ms, hour_bin_s=3600.0):
    start_ms = float(slice_start_ms)
    end_ms = float(slice_end_ms)
    if end_ms <= start_ms:
        raise ValueError("slice_end_ms must be later than slice_start_ms.")
    duration_s = (end_ms - start_ms) / 1000.0
    edges = [0.0]
    while edges[-1] + float(hour_bin_s) < duration_s:
        edges.append(edges[-1] + float(hour_bin_s))
    edges.append(duration_s)
    edges_s = np.asarray(edges, dtype=np.float64)
    centers_s = (edges_s[:-1] + edges_s[1:]) / 2.0
    n_hours = centers_s.size
    rt_mean = np.full(n_hours, np.nan, dtype=np.float32)
    rt_median = np.full(n_hours, np.nan, dtype=np.float32)
    rt_count = np.zeros(n_hours, dtype=np.int32)
    rt_class_fraction = np.full((n_hours, 3), np.nan, dtype=np.float32)
    dominant_rt_class = np.full(n_hours, -1, dtype=np.int8)

    sample_epoch = np.asarray([
        float(item["sample_start_epoch_ms"]) if item.get("sample_start_epoch_ms") is not None else np.nan
        for item in records
    ], dtype=np.float64)
    rt = np.asarray([float(item.get("rt_ms", np.nan)) for item in records], dtype=np.float64)
    rt_class = np.asarray([int(item.get("rt_class", -1)) for item in records], dtype=np.int8)
    rel_s = (sample_epoch - start_ms) / 1000.0

    for hour_idx in range(n_hours):
        left = edges_s[hour_idx]
        right = edges_s[hour_idx + 1]
        mask = (rel_s >= left) & (rel_s < right) & np.isfinite(rt)
        if not np.any(mask):
            continue
        selected = rt[mask]
        rt_mean[hour_idx] = float(np.nanmean(selected))
        rt_median[hour_idx] = float(np.nanmedian(selected))
        rt_count[hour_idx] = int(selected.size)
        class_counts = np.asarray([np.sum(rt_class[mask] == idx) for idx in range(3)], dtype=np.float64)
        total = float(np.sum(class_counts))
        if total > 0:
            rt_class_fraction[hour_idx] = (class_counts / total).astype(np.float32)
            dominant_rt_class[hour_idx] = int(np.argmax(class_counts))

    return {
        "hourly_time_s": centers_s,
        "hourly_edges_s": edges_s,
        "hourly_rt_mean_ms": rt_mean,
        "hourly_rt_median_ms": rt_median,
        "hourly_rt_count": rt_count,
        "hourly_rt_class_fraction": rt_class_fraction,
        "hourly_dominant_rt_class": dominant_rt_class,
    }


def fit_gaussian_mixture_1d(values, n_components=3, max_iter=200, tol=1e-6, random_seed=0):
    values = np.asarray(values, dtype=np.float64)
    values = values[np.isfinite(values)]
    if values.size < n_components:
        raise ValueError("Not enough values to fit Gaussian mixture.")
    quantiles = np.linspace(0.15, 0.85, n_components)
    means = np.quantile(values, quantiles)
    variance = max(float(np.nanvar(values)), 1e-6)
    sigmas = np.full(n_components, math.sqrt(variance), dtype=np.float64)
    weights = np.full(n_components, 1.0 / n_components, dtype=np.float64)
    last_ll = -np.inf
    converged = False
    for iteration in range(1, int(max_iter) + 1):
        density = np.vstack([
            weights[idx] * _normal_pdf(values, means[idx], sigmas[idx])
            for idx in range(n_components)
        ]).T
        denom = np.sum(density, axis=1, keepdims=True)
        resp = np.divide(density, denom, out=np.full_like(density, 1.0 / n_components), where=denom > 0)
        nk = np.sum(resp, axis=0) + 1e-12
        weights = nk / np.sum(nk)
        means = np.sum(resp * values[:, None], axis=0) / nk
        var = np.sum(resp * (values[:, None] - means[None, :]) ** 2, axis=0) / nk
        sigmas = np.sqrt(np.maximum(var, 1e-6))
        order = np.argsort(means)
        means = means[order]
        sigmas = sigmas[order]
        weights = weights[order]
        ll = float(np.sum(np.log(np.maximum(np.sum(density, axis=1), 1e-300))))
        if np.isfinite(last_ll) and abs(ll - last_ll) < float(tol):
            converged = True
            break
        last_ll = ll
    return {
        "means": means,
        "sigmas": sigmas,
        "weights": weights,
        "converged": converged,
        "n_iter": iteration,
    }


def gaussian_mixture_thresholds(means, sigmas, weights):
    means = np.asarray(means, dtype=np.float64)
    sigmas = np.asarray(sigmas, dtype=np.float64)
    weights = np.asarray(weights, dtype=np.float64)
    thresholds = []
    for idx in range(len(means) - 1):
        thresholds.append(_component_intersection(means[idx], sigmas[idx], weights[idx], means[idx + 1], sigmas[idx + 1], weights[idx + 1]))
    return np.asarray(thresholds, dtype=np.float64)


def _component_intersection(m1, s1, w1, m2, s2, w2):
    midpoint = (float(m1) + float(m2)) / 2.0
    a = 1.0 / (2.0 * s2 ** 2) - 1.0 / (2.0 * s1 ** 2)
    b = m1 / (s1 ** 2) - m2 / (s2 ** 2)
    c = (m2 ** 2) / (2.0 * s2 ** 2) - (m1 ** 2) / (2.0 * s1 ** 2) + math.log((w2 * s1) / max(w1 * s2, 1e-300))
    if abs(a) < 1e-12:
        if abs(b) < 1e-12:
            return midpoint
        root = -c / b
        return float(root) if m1 < root < m2 else midpoint
    disc = b * b - 4.0 * a * c
    if disc < 0:
        return midpoint
    roots = [(-b - math.sqrt(disc)) / (2.0 * a), (-b + math.sqrt(disc)) / (2.0 * a)]
    between = [root for root in roots if m1 < root < m2]
    if between:
        return float(between[0])
    return midpoint


def _normal_pdf(values, mean, sigma):
    sigma = max(float(sigma), 1e-6)
    z = (values - float(mean)) / sigma
    return np.exp(-0.5 * z * z) / (sigma * math.sqrt(2.0 * math.pi))


def _assign_rt_classes(records, classes, fit):
    out = []
    for item, cls in zip(records, classes):
        next_item = dict(item)
        next_item["rt_class"] = int(cls)
        next_item["rt_class_label"] = RT_CLASS_LABELS.get(int(cls), "unknown")
        if np.isfinite(next_item.get("rt_ms", np.nan)):
            next_item["perf_state"] = classify_perf_state(next_item.get("trial_smoothed_perf_pct", np.nan))
            next_item["bias_state"] = classify_bias_state(next_item.get("trial_smoothed_bias_score", np.nan))
        out.append(next_item)
    return out


def _empty_rt_fit(reason, cfg):
    return {
        "method": "log_rt_3_component_gaussian_mixture",
        "reason": reason,
        "n_valid_rt": 0,
        "thresholds_ms": [],
        "thresholds_log": [],
        "component_means_log": [],
        "component_sigmas_log": [],
        "component_weights": [],
        "component_means_ms": [],
        "converged": False,
        "n_iter": 0,
        "config": dict(cfg),
    }


def _parse_tevent_file(path):
    warnings = []
    events = {}
    if not path.exists():
        return events, [f"Tevent file not found: {path}"]
    with open(path, "r", encoding="utf-8", errors="replace") as handle:
        for line_no, line in enumerate(handle, start=1):
            line = line.replace("Tevent:", "").strip()
            if not line:
                continue
            try:
                tevent = TeventData(line)
                events[int(tevent.trial_num)] = tevent
            except Exception as exc:
                warnings.append(f"Failed to parse {path}:{line_no}: {exc}")
    return events, warnings


def _trial_relative_origin_ts(trial):
    start_ts = int(getattr(trial, "start_ts", 0) or 0)
    if start_ts > 0:
        return start_ts
    trial_start_timestamp_ms = getattr(trial, "trial_start_timestamp_ms", None)
    if trial_start_timestamp_ms is not None and np.isfinite(float(trial_start_timestamp_ms)):
        return float(trial_start_timestamp_ms)
    return 0.0
