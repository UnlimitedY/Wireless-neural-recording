import csv
import datetime as _dt
import json
import os
import re
import sys
import tempfile

os.environ.setdefault("MPLCONFIGDIR", os.path.join(tempfile.gettempdir(), "matplotlib"))
os.environ.setdefault("MPLBACKEND", "Agg")

import h5py
import numpy as np
import matplotlib

matplotlib.use("Agg")
from matplotlib import pyplot as plt

_PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir, os.pardir))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from Common_Analysis.behavior_analysis import (
    BIAS_STATE_LABELS,
    PERF_STATE_LABELS,
    RULE_LABELS,
    resolve_sd_card_dir_from_source_root,
)
from Common_Analysis.behavior_rt import RT_CLASS_LABELS, compute_hourly_rt_features, parse_rt_trials

from .config import resolve_config
from .connectivity_features import load_connectivity_sidecar
from .tz_compat import ZoneInfo


ANALYSIS_VERSION = 1
DEFAULT_N_PERMUTATIONS = 1000


def run_slice_statistical_analysis(
    h5_paths,
    output_dir,
    config=None,
    reference_scope="daily",
    n_permutations=DEFAULT_N_PERMUTATIONS,
    random_seed=0,
    make_figures=True,
):
    table = build_slice_statistics_table(h5_paths, config=config, reference_scope=reference_scope)
    models = fit_slice_statistical_models(
        table,
        n_permutations=n_permutations,
        random_seed=random_seed,
    )
    result = {
        "version": ANALYSIS_VERSION,
        "table": table,
        "models": models,
        "reference_scope": str(reference_scope or "daily"),
        "n_permutations": int(n_permutations),
        "random_seed": int(random_seed),
        "make_figures": bool(make_figures),
    }
    slice_id = table.get("slice_id") or slice_statistics_id_from_h5_paths(h5_paths)
    return write_slice_statistics_outputs(result, output_dir, slice_id, make_figures=make_figures)


def build_slice_statistics_table(h5_paths, config=None, reference_scope="daily"):
    cfg = resolve_config(config)
    paths = [os.path.abspath(str(path)) for path in h5_paths or [] if path]
    if not paths:
        raise ValueError("No H5 paths were provided for slice statistics.")

    pieces = []
    warnings = []
    reference_bands = None
    reference_states = None
    reference_occ_states = None
    slice_meta = None
    timezone_name = cfg.get("general.timezone", "Asia/Shanghai")
    for path in sorted(paths):
        try:
            piece = _read_h5_piece(path, cfg)
        except Exception as exc:
            warnings.append(f"Skipped {path}: {exc}")
            continue
        if reference_bands is None:
            reference_bands = piece["band_names"]
            reference_states = piece["state_names"]
            reference_occ_states = piece["state_names_occupancy"]
            slice_meta = piece["slice_meta"]
            timezone_name = piece["timezone_name"]
        elif piece["band_names"] != reference_bands or piece["state_names"] != reference_states:
            warnings.append(f"Skipped {path}: band/state names do not match the first H5.")
            continue
        pieces.append(piece)

    if not pieces:
        raise ValueError("No compatible H5 files with hourly rhythm data were available.")

    pieces = sorted(pieces, key=lambda item: (item["origin_epoch_ms"], item["path"]))
    global_origin_ms = min(piece["origin_epoch_ms"] for piece in pieces)
    tz = ZoneInfo(timezone_name)
    band_names = reference_bands or []
    state_names = reference_states or []
    state_names_occupancy = reference_occ_states or state_names

    hourly_feature = np.concatenate([piece["hourly_feature"] for piece in pieces], axis=0)
    hourly_count = np.concatenate([piece["hourly_count"] for piece in pieces], axis=0)
    hourly_norm_daily = np.concatenate([piece["hourly_norm_daily"] for piece in pieces], axis=0)
    state_occupancy = np.concatenate([piece["state_occupancy"] for piece in pieces], axis=0)
    hour_valid_mask = np.concatenate([piece["hour_valid_mask"] for piece in pieces], axis=0)
    epoch_ms = np.concatenate(
        [
            piece["origin_epoch_ms"] + np.asarray(piece["hourly_time_s"], dtype=np.float64) * 1000.0
            for piece in pieces
        ],
        axis=0,
    )
    source_h5 = np.concatenate(
        [np.full(piece["hourly_time_s"].shape, idx, dtype=np.int32) for idx, piece in enumerate(pieces)],
        axis=0,
    )

    state_norm_daily = None
    state_norm_slice = None
    if all(piece["state_stratified"] is not None and piece["state_stratified_count"] is not None for piece in pieces):
        state_stratified = np.concatenate([piece["state_stratified"] for piece in pieces], axis=0)
        state_stratified_count = np.concatenate([piece["state_stratified_count"] for piece in pieces], axis=0)
        state_norm_daily = np.concatenate([piece["state_norm_daily"] for piece in pieces], axis=0)
        state_norm_slice = _normalize_state_stratified_table(state_stratified, state_stratified_count)
    else:
        state_stratified = None
        state_stratified_count = None

    reference_scope = str(reference_scope or "daily").lower()
    if reference_scope not in {"daily", "whole_slice"}:
        reference_scope = "daily"
    if reference_scope == "whole_slice":
        hourly_norm = _normalize_hourly_feature_table(hourly_feature, hourly_count)
        state_norm = state_norm_slice
        lfp_suffix = "whole_slice_norm"
    else:
        hourly_norm = hourly_norm_daily
        state_norm = state_norm_daily
        lfp_suffix = "daily_norm"

    behavior_arrays = _concat_behavior_arrays(pieces)
    order = np.argsort(epoch_ms)
    epoch_ms = epoch_ms[order]
    source_h5 = source_h5[order]
    hourly_norm = hourly_norm[order]
    state_occupancy = state_occupancy[order]
    hour_valid_mask = hour_valid_mask[order]
    if state_norm is not None:
        state_norm = state_norm[order]
    for key in list(behavior_arrays.keys()):
        behavior_arrays[key] = behavior_arrays[key][order]

    n_channels = int(hourly_norm.shape[1]) if hourly_norm.ndim == 3 else 1
    group_defs, group_names = _normalize_channel_groups(
        cfg.get("rhythm.average_channel_groups", [list(range(n_channels))]),
        cfg.get("rhythm.average_group_names", ["All channels"]),
        n_channels,
    )
    group_band_values = _group_band_values(hourly_norm, group_defs)
    state_group_band_values = _state_group_band_values(state_norm, group_defs) if state_norm is not None else None
    rt_arrays, rt_metadata = _load_rt_arrays_for_table(
        pieces,
        epoch_ms,
        global_origin_ms,
        cfg,
        warnings,
    )
    connectivity_columns, connectivity_metadata = _load_connectivity_columns_for_table(
        pieces,
        epoch_ms,
        cfg,
        group_defs,
        group_names,
        warnings,
    )

    rows = []
    for hour_idx, hour_epoch_ms in enumerate(epoch_ms):
        local_dt = _dt.datetime.fromtimestamp(float(hour_epoch_ms) / 1000.0, tz=_dt.timezone.utc).astimezone(tz)
        zt_hour = local_dt.hour + local_dt.minute / 60.0 + local_dt.second / 3600.0
        elapsed_s = (float(hour_epoch_ms) - float(global_origin_ms)) / 1000.0
        row = {
            "hour_index": int(hour_idx),
            "source_h5_index": int(source_h5[hour_idx]),
            "source_h5": os.path.basename(pieces[int(source_h5[hour_idx])]["path"]),
            "time_s": float(elapsed_s),
            "epoch_ms": float(hour_epoch_ms),
            "iso_time": local_dt.isoformat(),
            "elapsed_day": float(elapsed_s / 86400.0),
            "day_index": int(np.floor(elapsed_s / 86400.0)),
            "zt_hour": float(zt_hour),
            "circadian_sin": float(np.sin(2.0 * np.pi * zt_hour / 24.0)),
            "circadian_cos": float(np.cos(2.0 * np.pi * zt_hour / 24.0)),
            "is_dark": int(_is_dark_hour(zt_hour, cfg)),
            "hour_valid": int(bool(hour_valid_mask[hour_idx])),
        }
        row.update(_behavior_row(behavior_arrays, hour_idx))
        row.update(_rt_behavior_row(rt_arrays, hour_idx))

        for state_idx, state_name in enumerate(state_names_occupancy):
            if state_idx < state_occupancy.shape[1]:
                row[f"occ_{_token(state_name)}"] = _safe_float(state_occupancy[hour_idx, state_idx])

        for group_idx, group_name in enumerate(group_names):
            for band_idx, band_name in enumerate(band_names):
                row[f"lfp_{_token(group_name)}_{_token(band_name)}_{lfp_suffix}"] = _safe_float(
                    group_band_values[hour_idx, group_idx, band_idx]
                )

        if state_group_band_values is not None:
            max_states = min(len(state_names), state_group_band_values.shape[1])
            for state_idx in range(max_states):
                state_name = state_names[state_idx]
                for group_idx, group_name in enumerate(group_names):
                    for band_idx, band_name in enumerate(band_names):
                        row[
                            f"state_{_token(state_name)}_{_token(group_name)}_{_token(band_name)}_{lfp_suffix}"
                        ] = _safe_float(state_group_band_values[hour_idx, state_idx, group_idx, band_idx])
        for column_name, values in connectivity_columns.items():
            row[column_name] = _safe_float(values[hour_idx])
        rows.append(row)

    numeric_columns = []
    if rows:
        numeric_columns = [
            key for key, value in rows[0].items()
            if key not in {"iso_time", "source_h5"} and isinstance(value, (int, float, np.integer, np.floating))
        ]

    path_list = [piece["path"] for piece in pieces]
    return {
        "rows": rows,
        "numeric_columns": numeric_columns,
        "target_columns": [
            col for col in numeric_columns
            if col.startswith("lfp_") or col.startswith("state_") or col.startswith("plv_") or col.startswith("pac_")
        ],
        "state_occupancy_columns": [col for col in numeric_columns if col.startswith("occ_")],
        "band_names": band_names,
        "state_names": state_names,
        "state_names_occupancy": state_names_occupancy,
        "group_names": group_names,
        "connectivity_metadata": connectivity_metadata,
        "rt_metadata": rt_metadata,
        "h5_paths": path_list,
        "h5_basenames": [os.path.basename(path) for path in path_list],
        "global_origin_epoch_ms": float(global_origin_ms),
        "timezone": timezone_name,
        "reference_scope": reference_scope,
        "lfp_suffix": lfp_suffix,
        "slice_meta": slice_meta or {},
        "slice_id": _slice_id_from_meta(slice_meta or {}, path_list),
        "warnings": warnings,
    }


def fit_slice_statistical_models(table, n_permutations=DEFAULT_N_PERMUTATIONS, random_seed=0):
    rows = table.get("rows", [])
    if not rows:
        return _empty_models()

    rng = np.random.default_rng(int(random_seed))
    target_columns = list(table.get("target_columns", []))
    all_state_occ_columns = list(table.get("state_occupancy_columns", []))
    state_occ_columns = all_state_occ_columns[:-1] if all_state_occ_columns else []

    base_columns = ["elapsed_day", "circadian_sin", "circadian_cos"] + state_occ_columns
    test_specs = _behavior_test_specs()
    day_ids = np.asarray([_to_float(row.get("day_index")) for row in rows], dtype=np.float64)
    n_days = len(np.unique(day_ids[np.isfinite(day_ids)]))
    low_confidence = n_days < 3

    term_rows = []
    for target in target_columns:
        for family, terms in test_specs:
            term_rows.append(
                _incremental_model_test(
                    rows,
                    target,
                    base_columns,
                    terms,
                    day_ids,
                    int(n_permutations),
                    rng,
                    test_family=family,
                    low_confidence=low_confidence,
                )
            )
    _assign_fdr_q_values(term_rows)
    term_rows = [_annotate_test_row(row, table, "lfp_behavior_modulation") for row in term_rows]

    effect_rows = _best_effect_rows(term_rows)
    lfp_behavior_rows = _sort_test_rows(term_rows)
    state_specific_rows = _build_state_specific_lfp_summary(lfp_behavior_rows)
    partial_rows = _fit_partial_correlations(
        rows,
        target_columns,
        base_columns,
        day_ids,
        int(n_permutations),
        rng,
        low_confidence=low_confidence,
    )
    _assign_fdr_q_values(partial_rows)
    partial_rows = [_annotate_test_row(row, table, "lfp_partial_behavior") for row in partial_rows]

    top_targets = [row["target"] for row in effect_rows[:10]]
    interaction_rows = _fit_interaction_screen(
        rows,
        top_targets,
        base_columns,
        day_ids,
        int(n_permutations),
        rng,
        low_confidence=low_confidence,
    )
    _assign_fdr_q_values(interaction_rows)
    interaction_rows = [_annotate_test_row(row, table, "lfp_behavior_x_circadian") for row in interaction_rows]

    occupancy_rows = []
    occupancy_base = ["elapsed_day", "circadian_sin", "circadian_cos"]
    for target in all_state_occ_columns:
        for family, terms in test_specs:
            occupancy_rows.append(
                _incremental_model_test(
                    rows,
                    target,
                    occupancy_base,
                    terms,
                    day_ids,
                    int(n_permutations),
                    rng,
                    test_family=family,
                    low_confidence=low_confidence,
                )
            )
    _assign_fdr_q_values(occupancy_rows)
    occupancy_rows = [
        _annotate_test_row(row, table, "natural_state_occupancy_modulation")
        for row in occupancy_rows
    ]

    return {
        "effect_summary": effect_rows,
        "term_tests": lfp_behavior_rows,
        "lfp_behavior_modulation": lfp_behavior_rows,
        "state_specific_lfp_summary": state_specific_rows,
        "partial_correlations": _sort_test_rows(partial_rows),
        "interaction_tests": _sort_test_rows(interaction_rows),
        "state_occupancy_tests": _sort_test_rows(occupancy_rows),
        "secondary_checks": _sort_test_rows(occupancy_rows),
        "base_columns": base_columns,
        "n_days": int(n_days),
        "low_confidence": bool(low_confidence),
        "n_permutations": int(n_permutations),
    }


def _behavior_test_specs():
    return [
        ("rule_block", ["rule_frequency", "rule_reversal_frequency"]),
        ("learning_stage_block", ["perf_intermediate", "perf_learned"]),
        ("bias_block", ["bias_left", "bias_right"]),
        ("rt_class_block", ["rt_middle", "rt_high"]),
        (
            "behavior_all",
            [
                "rule_frequency",
                "rule_reversal_frequency",
                "perf_intermediate",
                "perf_learned",
                "bias_left",
                "bias_right",
            ],
        ),
    ]


def _annotate_test_row(row, table, analysis_family):
    out = dict(row)
    out["analysis_family"] = str(analysis_family or "")
    out.update(_target_metadata(str(out.get("target", "")), table))
    return out


def _target_metadata(target, table):
    band_lookup = _token_lookup(table.get("band_names", []))
    group_lookup = _token_lookup(table.get("group_names", []))
    state_lookup = _token_lookup(table.get("state_names", []) + table.get("state_names_occupancy", []))
    suffix = str(table.get("lfp_suffix", "") or "")
    meta = {
        "target_class": "unknown",
        "natural_state": "",
        "natural_state_token": "",
        "channel_group": "",
        "channel_group_token": "",
        "band": "",
        "band_token": "",
        "feature_scope": suffix,
    }
    text = str(target or "")
    if text.startswith("lfp_"):
        meta["target_class"] = "global_lfp"
        meta["natural_state"] = "global"
        meta["natural_state_token"] = "global"
        _fill_group_band_meta(text[4:], group_lookup, band_lookup, meta)
    elif text.startswith("state_"):
        meta["target_class"] = "state_lfp"
        body = text[6:]
        state_token, state_name, remainder = _extract_prefix_token(body, state_lookup)
        meta["natural_state_token"] = state_token
        meta["natural_state"] = state_name
        _fill_group_band_meta(remainder, group_lookup, band_lookup, meta)
    elif text.startswith("occ_"):
        meta["target_class"] = "state_occupancy"
        meta["feature_scope"] = "hourly_fraction"
        state_token, state_name, _ = _extract_prefix_token(text[4:], state_lookup)
        meta["natural_state_token"] = state_token
        meta["natural_state"] = state_name
    elif text.startswith("plv_"):
        meta["target_class"] = "connectivity_plv"
        meta["natural_state"] = "connectivity"
        meta["natural_state_token"] = "connectivity"
        meta["feature_scope"] = "hourly_plv"
        band_token, band_name, remainder = _extract_prefix_token(text[4:], band_lookup)
        meta["band_token"] = band_token
        meta["band"] = band_name
        group_token, group_name, _ = _extract_prefix_token(remainder, group_lookup)
        meta["channel_group_token"] = group_token
        meta["channel_group"] = group_name or ("mean" if "mean" in text else "")
    elif text.startswith("pac_"):
        meta["target_class"] = "connectivity_pac"
        meta["natural_state"] = "connectivity"
        meta["natural_state_token"] = "connectivity"
        meta["feature_scope"] = "hourly_pac"
        meta.update(_pac_target_metadata(text[4:], band_lookup, group_lookup))
    return meta


def _pac_target_metadata(body, band_lookup, group_lookup):
    out = {
        "band_token": "",
        "band": "",
        "phase_band_token": "",
        "phase_band": "",
        "amplitude_band_token": "",
        "amplitude_band": "",
        "channel_group_token": "",
        "channel_group": "",
    }
    text = str(body or "")
    phase_token, phase_name, remainder = _extract_prefix_token(text, band_lookup)
    out["phase_band_token"] = phase_token
    out["phase_band"] = phase_name
    if remainder.startswith("to_"):
        remainder = remainder[3:]
    amp_token, amp_name, remainder = _extract_prefix_token(remainder, band_lookup)
    out["amplitude_band_token"] = amp_token
    out["amplitude_band"] = amp_name
    if phase_name or amp_name:
        out["band_token"] = f"{phase_token}_to_{amp_token}".strip("_")
        out["band"] = f"{phase_name}->{amp_name}".strip("->")
    group_token, group_name, _ = _extract_prefix_token(remainder, group_lookup)
    out["channel_group_token"] = group_token
    out["channel_group"] = group_name or ("mean" if "mean" in text else "")
    return out


def _fill_group_band_meta(body, group_lookup, band_lookup, meta):
    group_token, group_name, remainder = _extract_prefix_token(body, group_lookup)
    band_token, band_name, _ = _extract_prefix_token(remainder, band_lookup)
    if not band_token:
        band_token, band_name, _ = _find_any_token(body, band_lookup)
    if not group_token:
        group_token, group_name, _ = _find_any_token(body, group_lookup)
    meta["channel_group_token"] = group_token
    meta["channel_group"] = group_name
    meta["band_token"] = band_token
    meta["band"] = band_name


def _token_lookup(names):
    lookup = {}
    for name in names or []:
        token = _token(name)
        if token and token not in lookup:
            lookup[token] = str(name)
    return lookup


def _extract_prefix_token(text, lookup):
    value = str(text or "")
    for token in sorted(lookup.keys(), key=len, reverse=True):
        if value == token:
            return token, lookup[token], ""
        prefix = token + "_"
        if value.startswith(prefix):
            return token, lookup[token], value[len(prefix):]
    return "", "", value


def _find_any_token(text, lookup):
    value = str(text or "")
    for token in sorted(lookup.keys(), key=len, reverse=True):
        if token and (value == token or value.startswith(token + "_") or f"_{token}_" in f"_{value}_"):
            return token, lookup[token], value
    return "", "", value


def _build_state_specific_lfp_summary(lfp_rows):
    grouped = {}
    for row in lfp_rows or []:
        if row.get("target_class") not in {"global_lfp", "state_lfp"}:
            continue
        key = (
            row.get("channel_group_token", ""),
            row.get("band_token", ""),
            row.get("feature_scope", ""),
            row.get("test_family", ""),
        )
        grouped.setdefault(key, []).append(row)

    out = []
    for (_, _, feature_scope, test_family), rows in grouped.items():
        state_rows = [row for row in rows if row.get("target_class") == "state_lfp"]
        if not state_rows:
            continue
        sorted_states = _sort_test_rows(state_rows)
        top = sorted_states[0]
        global_rows = _sort_test_rows([row for row in rows if row.get("target_class") == "global_lfp"])
        global_row = global_rows[0] if global_rows else {}
        significant = [
            row for row in sorted_states
            if np.isfinite(_to_float(row.get("fdr_q"))) and _to_float(row.get("fdr_q")) < 0.05
        ]
        next_delta = _to_float(sorted_states[1].get("delta_r2")) if len(sorted_states) > 1 else np.nan
        top_delta = _to_float(top.get("delta_r2"))
        selectivity = top_delta - next_delta if np.isfinite(top_delta) and np.isfinite(next_delta) else np.nan
        interpretation = "insufficient_data"
        top_q = _to_float(top.get("fdr_q"))
        top_p = _to_float(top.get("permutation_p"))
        if np.isfinite(top_q) and top_q < 0.05:
            interpretation = "state_specific" if len(significant) == 1 else "multi_state"
        elif np.isfinite(top_p) and top_p < 0.05:
            interpretation = "nominal_exploratory"
        elif np.isfinite(top_delta):
            interpretation = "no_clear_state_specific_modulation"
        out.append(
            {
                "channel_group": top.get("channel_group", ""),
                "channel_group_token": top.get("channel_group_token", ""),
                "band": top.get("band", ""),
                "band_token": top.get("band_token", ""),
                "feature_scope": feature_scope,
                "test_family": test_family,
                "top_state": top.get("natural_state", ""),
                "top_state_token": top.get("natural_state_token", ""),
                "top_state_target": top.get("target", ""),
                "top_state_delta_r2": top.get("delta_r2", np.nan),
                "top_state_p": top.get("permutation_p", np.nan),
                "top_state_q": top.get("fdr_q", np.nan),
                "top_term": top.get("top_term", ""),
                "effect_sign": top.get("effect_sign", ""),
                "n_obs": top.get("n_obs", ""),
                "significant_states": ";".join(row.get("natural_state", "") for row in significant),
                "n_significant_states": int(len(significant)),
                "state_selectivity_delta_r2": selectivity,
                "global_target": global_row.get("target", ""),
                "global_delta_r2": global_row.get("delta_r2", np.nan),
                "global_q": global_row.get("fdr_q", np.nan),
                "interpretation": interpretation,
            }
        )
    return sorted(out, key=_state_specific_sort_key)


def _state_specific_sort_key(row):
    q = _to_float(row.get("top_state_q"))
    p = _to_float(row.get("top_state_p"))
    delta = _to_float(row.get("top_state_delta_r2"))
    q_key = q if np.isfinite(q) else np.inf
    p_key = p if np.isfinite(p) else np.inf
    delta_key = -delta if np.isfinite(delta) else np.inf
    return (q_key, p_key, delta_key, str(row.get("top_state_target", "")))


def write_slice_statistics_outputs(result, output_dir, slice_id, make_figures=True):
    output_dir = os.path.abspath(str(output_dir or os.getcwd()))
    os.makedirs(output_dir, exist_ok=True)
    safe_id = _sanitize_filename_component(slice_id or "slice")
    table = result["table"]
    models = result["models"]
    rows = table.get("rows", [])

    hourly_csv = os.path.join(output_dir, f"{safe_id}_slice_statistics_hourly_table.csv")
    effect_csv = os.path.join(output_dir, f"{safe_id}_slice_statistics_effect_summary.csv")
    term_csv = os.path.join(output_dir, f"{safe_id}_slice_statistics_term_tests.csv")
    lfp_behavior_csv = os.path.join(output_dir, f"{safe_id}_slice_statistics_lfp_behavior_modulation.csv")
    state_specific_csv = os.path.join(output_dir, f"{safe_id}_slice_statistics_state_specific_lfp_summary.csv")
    state_occupancy_csv = os.path.join(output_dir, f"{safe_id}_slice_statistics_state_occupancy_tests.csv")
    interaction_csv = os.path.join(output_dir, f"{safe_id}_slice_statistics_interaction_tests.csv")
    partial_csv = os.path.join(output_dir, f"{safe_id}_slice_statistics_partial_correlations.csv")
    summary_json = os.path.join(output_dir, f"{safe_id}_slice_statistics_summary.json")

    _write_csv(rows, hourly_csv)
    _write_csv(models.get("effect_summary", []), effect_csv)
    _write_csv(models.get("term_tests", []), term_csv)
    _write_csv(models.get("lfp_behavior_modulation", models.get("term_tests", [])), lfp_behavior_csv)
    _write_csv(models.get("state_specific_lfp_summary", []), state_specific_csv)
    _write_csv(models.get("state_occupancy_tests", []), state_occupancy_csv)
    _write_csv(models.get("interaction_tests", []), interaction_csv)
    _write_csv(models.get("partial_correlations", []), partial_csv)

    figure_paths = []
    if make_figures:
        figure_paths = make_slice_statistics_figures(result, output_dir, safe_id)

    summary = {
        "version": ANALYSIS_VERSION,
        "slice_id": safe_id,
        "reference_scope": result.get("reference_scope", table.get("reference_scope", "daily")),
        "n_permutations": int(result.get("n_permutations", models.get("n_permutations", 0))),
        "random_seed": int(result.get("random_seed", 0)),
        "n_hours": int(len(rows)),
        "n_days": int(models.get("n_days", 0)),
        "n_h5_files": int(len(table.get("h5_paths", []))),
        "low_confidence": bool(models.get("low_confidence", False)),
        "h5_paths": list(table.get("h5_paths", [])),
        "hourly_csv": hourly_csv,
        "effect_csv": effect_csv,
        "term_tests_csv": term_csv,
        "lfp_behavior_modulation_csv": lfp_behavior_csv,
        "state_specific_lfp_summary_csv": state_specific_csv,
        "state_occupancy_tests_csv": state_occupancy_csv,
        "interaction_tests_csv": interaction_csv,
        "partial_correlations_csv": partial_csv,
        "summary_json": summary_json,
        "figure_paths": figure_paths,
        "top_effects": models.get("effect_summary", [])[:20],
        "top_lfp_behavior_modulations": models.get("lfp_behavior_modulation", [])[:30],
        "top_state_specific_lfp": models.get("state_specific_lfp_summary", [])[:30],
        "top_state_occupancy_effects": models.get("state_occupancy_tests", [])[:30],
        "top_partial_correlations": models.get("partial_correlations", [])[:20],
        "top_interactions": models.get("interaction_tests", [])[:20],
        "top_secondary_checks": models.get("secondary_checks", [])[:20],
        "warnings": list(table.get("warnings", [])),
        "research_question": (
            "Long-term PFC LFP dynamics modulated by circadian phase, sleep/wake/cognitive state, "
            "task rule, learning stage, and choice bias."
        ),
        "interpretation_note": (
            "Single-animal, single-slice statistics are exploratory. Use significant FDR-corrected "
            "targets as candidates for cross-animal or trial-level validation."
        ),
    }
    with open(summary_json, "w", encoding="utf-8") as handle:
        json.dump(summary, handle, ensure_ascii=False, indent=2)
    return summary


def make_slice_statistics_figures(result, output_dir, slice_id):
    table = result["table"]
    models = result["models"]
    paths = []
    effects_path = os.path.join(output_dir, f"{slice_id}_slice_statistics_effects.png")
    heatmap_path = os.path.join(output_dir, f"{slice_id}_slice_statistics_circadian_heatmap.png")
    overlay_path = os.path.join(output_dir, f"{slice_id}_slice_statistics_behavior_lfp_overlay.png")
    state_specific_path = os.path.join(output_dir, f"{slice_id}_slice_statistics_state_specific_lfp.png")
    state_occupancy_path = os.path.join(output_dir, f"{slice_id}_slice_statistics_state_occupancy_effects.png")
    display_rows = models.get("lfp_behavior_modulation", []) or models.get("effect_summary", [])
    _plot_effects(display_rows, effects_path)
    _plot_circadian_heatmap(table, display_rows, heatmap_path)
    _plot_behavior_lfp_overlay(table, display_rows, overlay_path)
    _plot_state_specific_lfp(models.get("state_specific_lfp_summary", []), state_specific_path)
    _plot_state_occupancy_effects(models.get("state_occupancy_tests", []), state_occupancy_path)
    paths.extend([effects_path, heatmap_path, overlay_path, state_specific_path, state_occupancy_path])
    return paths


def slice_statistics_id_from_h5_paths(h5_paths):
    paths = [os.path.abspath(str(path)) for path in h5_paths or [] if path]
    if not paths:
        return "slice"
    try:
        meta = _read_slice_meta(paths[0])
    except Exception:
        meta = {}
    return _slice_id_from_meta(meta, paths)


def expected_slice_statistics_summary_path(h5_paths, output_dir):
    slice_id = slice_statistics_id_from_h5_paths(h5_paths)
    return os.path.join(
        os.path.abspath(str(output_dir or os.getcwd())),
        f"{_sanitize_filename_component(slice_id)}_slice_statistics_summary.json",
    )


def _read_h5_piece(path, cfg):
    with h5py.File(path, "r") as h5f:
        if "time" not in h5f or "hourly_time_s" not in h5f["time"]:
            raise ValueError("missing /time/hourly_time_s")
        if "rhythm" not in h5f or "hourly_feature_table" not in h5f["rhythm"]:
            raise ValueError("missing /rhythm/hourly_feature_table")
        meta = h5f["metadata"].attrs if "metadata" in h5f else {}
        time_attrs = h5f["time"].attrs
        origin = float(time_attrs.get("time_origin_epoch_ms", meta.get("time_origin_epoch_ms", np.nan)))
        if not np.isfinite(origin):
            raise ValueError("missing finite time origin")
        timezone_name = str(meta.get("timezone", cfg.get("general.timezone", "Asia/Shanghai")))
        hourly_time_s = np.asarray(h5f["time/hourly_time_s"][:], dtype=np.float64)
        hourly_feature = np.asarray(h5f["rhythm/hourly_feature_table"][:], dtype=np.float64)
        hourly_count = (
            np.asarray(h5f["rhythm/hourly_feature_count"][:], dtype=np.float64)
            if "hourly_feature_count" in h5f["rhythm"]
            else np.isfinite(hourly_feature).astype(np.float64)
        )
        hour_valid_mask = (
            np.asarray(h5f["rhythm/hour_valid_mask"][:], dtype=bool)
            if "hour_valid_mask" in h5f["rhythm"]
            else np.isfinite(hourly_feature).any(axis=tuple(range(1, hourly_feature.ndim)))
        )
        if hour_valid_mask.shape[0] != hourly_feature.shape[0]:
            hour_valid_mask = np.ones((hourly_feature.shape[0],), dtype=bool)
        hourly_feature = np.array(hourly_feature, copy=True)
        hourly_count = np.array(hourly_count, copy=True)
        hourly_feature[~hour_valid_mask] = np.nan
        hourly_count[~hour_valid_mask] = 0
        hourly_norm_daily = (
            np.asarray(h5f["rhythm/hourly_feature_table_normalized"][:], dtype=np.float64)
            if "hourly_feature_table_normalized" in h5f["rhythm"]
            else _normalize_hourly_feature_table(hourly_feature, hourly_count)
        )
        hourly_norm_daily = np.array(hourly_norm_daily, copy=True)
        hourly_norm_daily[~hour_valid_mask] = np.nan
        state_occupancy = (
            np.asarray(h5f["rhythm/state_occupancy_table"][:], dtype=np.float64)
            if "state_occupancy_table" in h5f["rhythm"]
            else np.zeros((hourly_feature.shape[0], 0), dtype=np.float64)
        )
        state_occupancy = np.array(state_occupancy, copy=True)
        if state_occupancy.shape[0] == hour_valid_mask.shape[0]:
            state_occupancy[~hour_valid_mask] = np.nan
        state_stratified = (
            np.asarray(h5f["rhythm/state_stratified_feature_table"][:], dtype=np.float64)
            if "state_stratified_feature_table" in h5f["rhythm"]
            else None
        )
        state_stratified_count = (
            np.asarray(h5f["rhythm/state_stratified_count"][:], dtype=np.float64)
            if "state_stratified_count" in h5f["rhythm"]
            else None
        )
        state_norm_daily = (
            np.asarray(h5f["rhythm/state_stratified_feature_table_normalized"][:], dtype=np.float64)
            if "state_stratified_feature_table_normalized" in h5f["rhythm"]
            else None
        )
        if state_stratified is not None:
            state_stratified = np.array(state_stratified, copy=True)
            state_stratified[~hour_valid_mask] = np.nan
        if state_stratified_count is not None:
            state_stratified_count = np.array(state_stratified_count, copy=True)
            state_stratified_count[~hour_valid_mask] = 0
        if state_norm_daily is None and state_stratified is not None and state_stratified_count is not None:
            state_norm_daily = _normalize_state_stratified_table(state_stratified, state_stratified_count)
        if state_norm_daily is not None:
            state_norm_daily = np.array(state_norm_daily, copy=True)
            state_norm_daily[~hour_valid_mask] = np.nan

        band_names = _read_string_dataset(h5f["rhythm/band_names"]) if "band_names" in h5f["rhythm"] else []
        state_names = _read_string_dataset(h5f["rhythm/state_names"]) if "state_names" in h5f["rhythm"] else []
        state_occ_names = (
            _read_string_dataset(h5f["rhythm/state_names_occupancy"])
            if "state_names_occupancy" in h5f["rhythm"]
            else state_names
        )
        behavior_arrays = _read_behavior_arrays(h5f["behavior"] if "behavior" in h5f else None, hourly_feature.shape[0])
        slice_meta = _read_slice_meta_from_attrs(meta)

    return {
        "path": os.path.abspath(path),
        "origin_epoch_ms": float(origin),
        "timezone_name": timezone_name,
        "hourly_time_s": hourly_time_s,
        "hourly_feature": hourly_feature,
        "hourly_count": hourly_count,
        "hourly_norm_daily": hourly_norm_daily,
        "state_occupancy": state_occupancy,
        "state_stratified": state_stratified,
        "state_stratified_count": state_stratified_count,
        "state_norm_daily": state_norm_daily,
        "hour_valid_mask": hour_valid_mask,
        "band_names": band_names,
        "state_names": state_names,
        "state_names_occupancy": state_occ_names,
        "behavior": behavior_arrays,
        "slice_meta": slice_meta,
    }


def _read_slice_meta(path):
    with h5py.File(path, "r") as h5f:
        meta = h5f["metadata"].attrs if "metadata" in h5f else {}
        return _read_slice_meta_from_attrs(meta)


def _read_slice_meta_from_attrs(meta):
    return {
        "source_root": str(meta.get("slice_source_root", "")),
        "slice_json_path": str(meta.get("slice_json_path", "")),
        "parent_slice_id": str(meta.get("parent_slice_id", meta.get("slice_id", ""))),
        "parent_slice_label": str(meta.get("parent_slice_label", meta.get("slice_label", ""))),
        "slice_label": str(meta.get("slice_label", "")),
    }


def _read_behavior_arrays(behavior, n_hours):
    defaults = {
        "hourly_rule": np.full(n_hours, -1, dtype=np.int16),
        "hourly_perf_pct": np.full(n_hours, np.nan, dtype=np.float32),
        "hourly_perf_state": np.full(n_hours, -1, dtype=np.int8),
        "hourly_bias_score": np.full(n_hours, np.nan, dtype=np.float32),
        "hourly_bias_state": np.full(n_hours, -1, dtype=np.int8),
        "hourly_trial_count": np.zeros(n_hours, dtype=np.int32),
        "hourly_valid_perf_count": np.zeros(n_hours, dtype=np.int32),
        "hourly_choice_count": np.zeros(n_hours, dtype=np.int32),
        "hourly_fill_source": np.zeros(n_hours, dtype=np.int8),
    }
    if behavior is None:
        return defaults
    for key in list(defaults.keys()):
        if key in behavior:
            defaults[key] = _fit_length(np.asarray(behavior[key][:]), n_hours, defaults[key])
    return defaults


def _concat_behavior_arrays(pieces):
    keys = list(pieces[0]["behavior"].keys())
    return {key: np.concatenate([piece["behavior"][key] for piece in pieces], axis=0) for key in keys}


def _behavior_row(arrays, hour_idx):
    rule = int(arrays["hourly_rule"][hour_idx])
    perf_state = int(arrays["hourly_perf_state"][hour_idx])
    bias_state = int(arrays["hourly_bias_state"][hour_idx])
    return {
        "rule_code": rule,
        "rule_label": RULE_LABELS.get(rule, "unknown"),
        "perf_pct": _safe_float(arrays["hourly_perf_pct"][hour_idx]),
        "perf_state_code": perf_state,
        "perf_state_label": PERF_STATE_LABELS.get(perf_state, "unknown"),
        "bias_score": _safe_float(arrays["hourly_bias_score"][hour_idx]),
        "bias_state_code": bias_state,
        "bias_state_label": BIAS_STATE_LABELS.get(bias_state, "unknown"),
        "trial_count": int(arrays["hourly_trial_count"][hour_idx]),
        "valid_perf_count": int(arrays["hourly_valid_perf_count"][hour_idx]),
        "choice_count": int(arrays["hourly_choice_count"][hour_idx]),
        "behavior_fill_source": int(arrays["hourly_fill_source"][hour_idx]),
        "rule_frequency": int(rule == 1),
        "rule_reversal_frequency": int(rule == 2),
        "perf_intermediate": int(perf_state == 1),
        "perf_learned": int(perf_state == 2),
        "bias_left": int(bias_state == 0),
        "bias_right": int(bias_state == 2),
    }


def _empty_rt_arrays(n_hours):
    return {
        "rt_mean_ms": np.full(n_hours, np.nan, dtype=np.float64),
        "rt_median_ms": np.full(n_hours, np.nan, dtype=np.float64),
        "rt_count": np.zeros(n_hours, dtype=np.int32),
        "rt_class_code": np.full(n_hours, -1, dtype=np.int8),
        "rt_low_fraction": np.full(n_hours, np.nan, dtype=np.float64),
        "rt_middle_fraction": np.full(n_hours, np.nan, dtype=np.float64),
        "rt_high_fraction": np.full(n_hours, np.nan, dtype=np.float64),
    }


def _load_rt_arrays_for_table(pieces, epoch_ms, global_origin_ms, cfg, warnings):
    arrays = _empty_rt_arrays(len(epoch_ms))
    metadata = {
        "available": False,
        "n_valid_rt": 0,
        "thresholds_ms": [],
        "source_root": "",
    }
    source_root = ""
    for piece in pieces:
        source_root = str(piece.get("slice_meta", {}).get("source_root", "") or source_root)
        if source_root:
            break
    if not source_root:
        return arrays, metadata
    try:
        sd_card_dir = resolve_sd_card_dir_from_source_root(source_root)
    except Exception as exc:
        warnings.append(f"RT features skipped: could not locate SD_card_files from {source_root}: {exc}")
        return arrays, metadata
    end_ms = float(np.nanmax(epoch_ms) + 1800000.0) if len(epoch_ms) else float(global_origin_ms)
    if end_ms <= float(global_origin_ms):
        return arrays, metadata
    try:
        records, fit, rt_warnings = parse_rt_trials(
            sd_card_dir,
            slice_start_ms=float(global_origin_ms),
            slice_end_ms=end_ms,
            config=cfg.get("behavior.rt", {}),
        )
        hourly = compute_hourly_rt_features(records, float(global_origin_ms), end_ms)
    except Exception as exc:
        warnings.append(f"RT features skipped: {exc}")
        return arrays, metadata
    if rt_warnings:
        warnings.extend(rt_warnings[:10])
        if len(rt_warnings) > 10:
            warnings.append(f"RT parsing emitted {len(rt_warnings) - 10} additional warning(s).")
    centers = float(global_origin_ms) + np.asarray(hourly["hourly_time_s"], dtype=np.float64) * 1000.0
    mapping = _nearest_hour_mapping(epoch_ms, centers, tolerance_ms=1800000.0 + 1.0)
    for src_idx, dst_idx in enumerate(mapping):
        if dst_idx < 0:
            continue
        arrays["rt_mean_ms"][dst_idx] = _safe_float(hourly["hourly_rt_mean_ms"][src_idx])
        arrays["rt_median_ms"][dst_idx] = _safe_float(hourly["hourly_rt_median_ms"][src_idx])
        arrays["rt_count"][dst_idx] = int(hourly["hourly_rt_count"][src_idx])
        arrays["rt_class_code"][dst_idx] = int(hourly["hourly_dominant_rt_class"][src_idx])
        fractions = np.asarray(hourly["hourly_rt_class_fraction"][src_idx], dtype=np.float64)
        if fractions.size >= 3:
            arrays["rt_low_fraction"][dst_idx] = _safe_float(fractions[0])
            arrays["rt_middle_fraction"][dst_idx] = _safe_float(fractions[1])
            arrays["rt_high_fraction"][dst_idx] = _safe_float(fractions[2])
    metadata.update(
        {
            "available": True,
            "n_valid_rt": int(fit.get("n_valid_rt", 0)) if isinstance(fit, dict) else 0,
            "thresholds_ms": [float(value) for value in fit.get("thresholds_ms", [])] if isinstance(fit, dict) else [],
            "source_root": source_root,
        }
    )
    return arrays, metadata


def _nearest_hour_mapping(target_epoch_ms, source_epoch_ms, tolerance_ms):
    target = np.asarray(target_epoch_ms, dtype=np.float64)
    source = np.asarray(source_epoch_ms, dtype=np.float64)
    out = np.full(source.shape, -1, dtype=np.int64)
    if target.size == 0 or source.size == 0:
        return out
    order = np.argsort(target)
    sorted_target = target[order]
    for idx, value in enumerate(source):
        if not np.isfinite(value):
            continue
        pos = int(np.searchsorted(sorted_target, value))
        candidates = []
        if pos < sorted_target.size:
            candidates.append(pos)
        if pos > 0:
            candidates.append(pos - 1)
        if not candidates:
            continue
        best = min(candidates, key=lambda item: abs(sorted_target[item] - value))
        if abs(sorted_target[best] - value) <= float(tolerance_ms):
            out[idx] = int(order[best])
    return out


def _rt_behavior_row(arrays, hour_idx):
    cls = int(arrays["rt_class_code"][hour_idx])
    valid_class = cls in {0, 1, 2}
    return {
        "rt_mean_ms": _safe_float(arrays["rt_mean_ms"][hour_idx]),
        "rt_median_ms": _safe_float(arrays["rt_median_ms"][hour_idx]),
        "rt_count": int(arrays["rt_count"][hour_idx]),
        "rt_class_code": cls,
        "rt_class_label": RT_CLASS_LABELS.get(cls, "unknown"),
        "rt_low_fraction": _safe_float(arrays["rt_low_fraction"][hour_idx]),
        "rt_middle_fraction": _safe_float(arrays["rt_middle_fraction"][hour_idx]),
        "rt_high_fraction": _safe_float(arrays["rt_high_fraction"][hour_idx]),
        "rt_middle": int(cls == 1) if valid_class else np.nan,
        "rt_high": int(cls == 2) if valid_class else np.nan,
    }


def _load_connectivity_columns_for_table(pieces, epoch_ms, cfg, group_defs, group_names, warnings):
    columns = {}
    metadata = {
        "available": False,
        "n_loaded": 0,
        "n_skipped": 0,
        "default_targets": "mean_and_channel_group_pair_connectivity",
        "include_channel_pair_targets": bool(cfg.get("connectivity.include_channel_pair_targets", False)),
    }
    for piece in pieces:
        sidecar = None
        for output_dir in _connectivity_candidate_dirs(piece["path"]):
            try:
                sidecar = load_connectivity_sidecar(piece["path"], output_dir)
            except Exception:
                sidecar = None
            if sidecar is not None:
                break
        if sidecar is None:
            metadata["n_skipped"] += 1
            continue
        metadata["n_loaded"] += 1
        _merge_connectivity_sidecar_columns(
            columns,
            epoch_ms,
            sidecar,
            group_defs,
            group_names,
            include_pair_targets=metadata["include_channel_pair_targets"],
        )
    metadata["available"] = metadata["n_loaded"] > 0
    if metadata["n_skipped"] and metadata["n_loaded"]:
        warnings.append(
            f"Connectivity sidecars loaded for {metadata['n_loaded']} H5 file(s); "
            f"{metadata['n_skipped']} H5 file(s) had no sidecar."
        )
    return columns, metadata


def _connectivity_candidate_dirs(h5_path):
    path = os.path.abspath(str(h5_path))
    dirs = [os.path.dirname(path)]
    parent = os.path.dirname(os.path.dirname(path))
    if parent and parent not in dirs:
        dirs.append(parent)
    return dirs


def _merge_connectivity_sidecar_columns(columns, epoch_ms, sidecar, group_defs, group_names, include_pair_targets=False):
    centers = np.asarray(sidecar.get("hour_center_epoch_ms", []), dtype=np.float64)
    mapping = _nearest_hour_mapping(epoch_ms, centers, tolerance_ms=1800000.0 + 1.0)
    if mapping.size == 0:
        return
    meta = sidecar.get("metadata", {})
    band_names = [str(name) for name in meta.get("band_names", [])]
    pac_pairs = list(meta.get("pac_band_pairs", []))
    plv = np.asarray(sidecar.get("plv", np.empty((0, 0, 0, 0))), dtype=np.float64)
    pac = np.asarray(sidecar.get("pac", np.empty((0, 0, 0, 0))), dtype=np.float64)
    if plv.ndim == 4 and plv.shape[0] == mapping.size:
        for band_idx, band_name in enumerate(band_names[: plv.shape[1]]):
            _fill_connectivity_column(
                columns,
                f"plv_{_token(band_name)}_mean",
                mapping,
                _connectivity_matrix_mean(plv[:, band_idx], off_diagonal=True),
                len(epoch_ms),
            )
            for group_a, name_a in zip(group_defs, group_names):
                for group_b, name_b in zip(group_defs, group_names):
                    values = _connectivity_group_pair_mean(plv[:, band_idx], group_a, group_b, off_diagonal=True)
                    _fill_connectivity_column(
                        columns,
                        f"plv_{_token(band_name)}_{_token(name_a)}_to_{_token(name_b)}",
                        mapping,
                        values,
                        len(epoch_ms),
                    )
            if include_pair_targets:
                for ch_a in range(plv.shape[2]):
                    for ch_b in range(plv.shape[3]):
                        _fill_connectivity_column(
                            columns,
                            f"plv_{_token(band_name)}_ch{ch_a:02d}_to_ch{ch_b:02d}",
                            mapping,
                            plv[:, band_idx, ch_a, ch_b],
                            len(epoch_ms),
                        )
    if pac.ndim == 4 and pac.shape[0] == mapping.size:
        for pair_idx, pair in enumerate(pac_pairs[: pac.shape[1]]):
            phase_band = str(pair.get("phase_band", f"phase{pair_idx}"))
            amp_band = str(pair.get("amplitude_band", f"amp{pair_idx}"))
            prefix = f"pac_{_token(phase_band)}_to_{_token(amp_band)}"
            _fill_connectivity_column(
                columns,
                f"{prefix}_mean",
                mapping,
                _connectivity_matrix_mean(pac[:, pair_idx], off_diagonal=False),
                len(epoch_ms),
            )
            for group_a, name_a in zip(group_defs, group_names):
                for group_b, name_b in zip(group_defs, group_names):
                    values = _connectivity_group_pair_mean(pac[:, pair_idx], group_a, group_b, off_diagonal=False)
                    _fill_connectivity_column(
                        columns,
                        f"{prefix}_{_token(name_a)}_phase_to_{_token(name_b)}_amp",
                        mapping,
                        values,
                        len(epoch_ms),
                    )
            if include_pair_targets:
                for ch_a in range(pac.shape[2]):
                    for ch_b in range(pac.shape[3]):
                        _fill_connectivity_column(
                            columns,
                            f"{prefix}_ch{ch_a:02d}_phase_to_ch{ch_b:02d}_amp",
                            mapping,
                            pac[:, pair_idx, ch_a, ch_b],
                            len(epoch_ms),
                        )


def _fill_connectivity_column(columns, name, mapping, values, n_hours):
    if name not in columns:
        columns[name] = np.full(int(n_hours), np.nan, dtype=np.float64)
    values = np.asarray(values, dtype=np.float64)
    for src_idx, dst_idx in enumerate(mapping):
        if dst_idx < 0 or src_idx >= values.size:
            continue
        columns[name][int(dst_idx)] = _safe_float(values[src_idx])


def _connectivity_matrix_mean(values, off_diagonal=False):
    arr = np.asarray(values, dtype=np.float64)
    if arr.ndim != 3:
        return np.full((arr.shape[0] if arr.ndim else 0,), np.nan, dtype=np.float64)
    mask = np.ones(arr.shape[-2:], dtype=bool)
    if off_diagonal and mask.shape[0] == mask.shape[1]:
        np.fill_diagonal(mask, False)
    flat = arr[:, mask]
    count = np.sum(np.isfinite(flat), axis=1)
    return np.divide(np.nansum(flat, axis=1), count, out=np.full(arr.shape[0], np.nan), where=count > 0)


def _connectivity_group_pair_mean(values, group_a, group_b, off_diagonal=False):
    arr = np.asarray(values, dtype=np.float64)
    if arr.ndim != 3:
        return np.full((arr.shape[0] if arr.ndim else 0,), np.nan, dtype=np.float64)
    a = [idx for idx in group_a if 0 <= int(idx) < arr.shape[1]]
    b = [idx for idx in group_b if 0 <= int(idx) < arr.shape[2]]
    if not a or not b:
        return np.full(arr.shape[0], np.nan, dtype=np.float64)
    sub = arr[:, a][:, :, b]
    mask = np.ones(sub.shape[1:], dtype=bool)
    if off_diagonal and a == b and mask.shape[0] == mask.shape[1]:
        np.fill_diagonal(mask, False)
    flat = sub[:, mask]
    count = np.sum(np.isfinite(flat), axis=1)
    return np.divide(np.nansum(flat, axis=1), count, out=np.full(arr.shape[0], np.nan), where=count > 0)


def _incremental_model_test(
    rows,
    target,
    base_columns,
    add_columns,
    day_ids,
    n_permutations,
    rng,
    test_family,
    low_confidence=False,
):
    y = _column(rows, target)
    base_raw = _matrix(rows, base_columns)
    add_raw = _matrix(rows, add_columns)
    valid = np.isfinite(y) & np.all(np.isfinite(base_raw), axis=1) & np.all(np.isfinite(add_raw), axis=1)
    n_obs = int(np.sum(valid))
    row = {
        "target": target,
        "test_family": test_family,
        "n_obs": n_obs,
        "r2_base": np.nan,
        "r2_full": np.nan,
        "delta_r2": np.nan,
        "permutation_p": np.nan,
        "fdr_q": np.nan,
        "top_term": "",
        "top_beta": np.nan,
        "effect_sign": "",
        "low_confidence": int(bool(low_confidence)),
        "base_terms": json.dumps(base_columns, ensure_ascii=False),
        "test_terms": json.dumps(add_columns, ensure_ascii=False),
    }
    min_obs = max(24, len(base_columns) + len(add_columns) + 4)
    if n_obs < min_obs:
        return row

    y_fit = y[valid]
    base_fit = _standardize_design(np.column_stack([np.ones(n_obs), base_raw[valid]]))
    full_fit = _standardize_design(np.column_stack([np.ones(n_obs), base_raw[valid], add_raw[valid]]))
    base_model = _fit_ols(y_fit, base_fit)
    full_model = _fit_ols(y_fit, full_fit)
    beta = full_model["beta"][-len(add_columns):] if add_columns else np.zeros((0,))
    if beta.size:
        top_idx = int(np.nanargmax(np.abs(beta)))
        row["top_term"] = add_columns[top_idx]
        row["top_beta"] = float(beta[top_idx])
        row["effect_sign"] = "positive" if beta[top_idx] > 0 else "negative" if beta[top_idx] < 0 else "zero"
    row["r2_base"] = float(base_model["r2"])
    row["r2_full"] = float(full_model["r2"])
    row["delta_r2"] = float(max(0.0, full_model["r2"] - base_model["r2"]))

    if not low_confidence and n_permutations > 0 and row["delta_r2"] >= 0:
        perm_count = 0
        extreme = 0
        for _ in range(int(n_permutations)):
            perm_add = _permute_by_day_blocks(add_raw, day_ids, rng)
            perm_valid = valid & np.all(np.isfinite(perm_add), axis=1)
            if int(np.sum(perm_valid)) < min_obs:
                continue
            perm_n = int(np.sum(perm_valid))
            perm_y = y[perm_valid]
            perm_base = _standardize_design(np.column_stack([np.ones(perm_n), base_raw[perm_valid]]))
            perm_full = _standardize_design(np.column_stack([np.ones(perm_n), base_raw[perm_valid], perm_add[perm_valid]]))
            perm_delta = max(0.0, _fit_ols(perm_y, perm_full)["r2"] - _fit_ols(perm_y, perm_base)["r2"])
            extreme += int(perm_delta >= row["delta_r2"] - 1e-12)
            perm_count += 1
        if perm_count > 0:
            row["permutation_p"] = float((extreme + 1.0) / (perm_count + 1.0))
    return row


def _fit_partial_correlations(rows, target_columns, base_columns, day_ids, n_permutations, rng, low_confidence):
    specs = [
        ("perf_pct_partial", "perf_pct", base_columns + ["rule_frequency", "rule_reversal_frequency", "bias_left", "bias_right"]),
        ("bias_score_partial", "bias_score", base_columns + ["rule_frequency", "rule_reversal_frequency", "perf_intermediate", "perf_learned"]),
        (
            "rt_median_ms_partial",
            "rt_median_ms",
            base_columns + [
                "rule_frequency",
                "rule_reversal_frequency",
                "perf_intermediate",
                "perf_learned",
                "bias_left",
                "bias_right",
            ],
        ),
    ]
    out = []
    for target in target_columns:
        for family, term, covariates in specs:
            out.append(
                _partial_correlation_test(
                    rows,
                    target,
                    term,
                    covariates,
                    day_ids,
                    n_permutations,
                    rng,
                    family,
                    low_confidence,
                )
            )
    return out


def _partial_correlation_test(rows, target, term, covariates, day_ids, n_permutations, rng, family, low_confidence):
    y = _column(rows, target)
    x = _column(rows, term)
    cov = _matrix(rows, covariates)
    valid = np.isfinite(y) & np.isfinite(x) & np.all(np.isfinite(cov), axis=1)
    n_obs = int(np.sum(valid))
    row = {
        "target": target,
        "test_family": family,
        "n_obs": n_obs,
        "partial_r": np.nan,
        "permutation_p": np.nan,
        "fdr_q": np.nan,
        "top_term": term,
        "effect_sign": "",
        "low_confidence": int(bool(low_confidence)),
        "base_terms": json.dumps(covariates, ensure_ascii=False),
        "test_terms": json.dumps([term], ensure_ascii=False),
    }
    if n_obs < max(24, len(covariates) + 4):
        return row
    y_res = _residualize(y[valid], cov[valid])
    x_res = _residualize(x[valid], cov[valid])
    r = _pearson_r(y_res, x_res)
    row["partial_r"] = float(r)
    row["effect_sign"] = "positive" if r > 0 else "negative" if r < 0 else "zero"
    if not low_confidence and n_permutations > 0 and np.isfinite(r):
        extreme = 0
        perm_count = 0
        for _ in range(int(n_permutations)):
            x_perm_full = _permute_by_day_blocks(x[:, np.newaxis], day_ids, rng)[:, 0]
            perm_valid = valid & np.isfinite(x_perm_full)
            if int(np.sum(perm_valid)) < max(24, len(covariates) + 4):
                continue
            y_perm_res = _residualize(y[perm_valid], cov[perm_valid])
            x_perm_res = _residualize(x_perm_full[perm_valid], cov[perm_valid])
            perm_r = _pearson_r(y_perm_res, x_perm_res)
            extreme += int(abs(perm_r) >= abs(r) - 1e-12)
            perm_count += 1
        if perm_count > 0:
            row["permutation_p"] = float((extreme + 1.0) / (perm_count + 1.0))
    return row


def _fit_interaction_screen(rows, top_targets, base_columns, day_ids, n_permutations, rng, low_confidence):
    behavior_terms = [
        "rule_frequency",
        "rule_reversal_frequency",
        "perf_learned",
        "bias_left",
        "bias_right",
    ]
    has_rt_terms = _column_has_finite(rows, "rt_high") and _column_has_finite(rows, "rt_middle")
    if has_rt_terms:
        behavior_terms.append("rt_high")
    interaction_columns = []
    augmented_rows = []
    for row in rows:
        augmented = dict(row)
        for term in behavior_terms:
            for circadian_term in ["circadian_sin", "circadian_cos"]:
                name = f"{term}_x_{circadian_term}"
                augmented[name] = _to_float(row.get(term)) * _to_float(row.get(circadian_term))
                if name not in interaction_columns:
                    interaction_columns.append(name)
        augmented_rows.append(augmented)
    base_with_behavior = base_columns + [
        "rule_frequency",
        "rule_reversal_frequency",
        "perf_intermediate",
        "perf_learned",
        "bias_left",
        "bias_right",
    ]
    if has_rt_terms:
        base_with_behavior.extend(["rt_middle", "rt_high"])
    out = []
    for target in top_targets:
        out.append(
            _incremental_model_test(
                augmented_rows,
                target,
                base_with_behavior,
                interaction_columns,
                day_ids,
                n_permutations,
                rng,
                test_family="behavior_x_circadian",
                low_confidence=low_confidence,
            )
        )
    return out


def _best_effect_rows(term_rows):
    by_target = {}
    for row in term_rows:
        target = row.get("target", "")
        if not target:
            continue
        current = by_target.get(target)
        if current is None or _row_sort_key(row) < _row_sort_key(current):
            by_target[target] = dict(row)
    return _sort_test_rows(list(by_target.values()))


def _sort_test_rows(rows):
    return sorted(rows, key=_row_sort_key)


def _row_sort_key(row):
    q = _to_float(row.get("fdr_q"))
    p = _to_float(row.get("permutation_p"))
    delta = _to_float(row.get("delta_r2"))
    if not np.isfinite(delta):
        delta = abs(_to_float(row.get("partial_r")))
    q_key = q if np.isfinite(q) else np.inf
    p_key = p if np.isfinite(p) else np.inf
    delta_key = -delta if np.isfinite(delta) else np.inf
    return (q_key, p_key, delta_key, str(row.get("target", "")))


def _assign_fdr_q_values(rows):
    pvals = np.asarray([_to_float(row.get("permutation_p")) for row in rows], dtype=np.float64)
    valid = np.isfinite(pvals)
    if not np.any(valid):
        return rows
    idx = np.flatnonzero(valid)
    order = idx[np.argsort(pvals[idx])]
    m = float(order.size)
    q_ordered = np.empty(order.size, dtype=np.float64)
    running = 1.0
    for rank_rev, row_idx in enumerate(order[::-1], start=1):
        rank = order.size - rank_rev + 1
        q_val = min(running, pvals[row_idx] * m / float(rank))
        running = q_val
        q_ordered[rank - 1] = q_val
    for pos, row_idx in enumerate(order):
        rows[int(row_idx)]["fdr_q"] = float(min(1.0, q_ordered[pos]))
    return rows


def _matrix(rows, columns):
    if not columns:
        return np.zeros((len(rows), 0), dtype=np.float64)
    return np.asarray([[_to_float(row.get(column)) for column in columns] for row in rows], dtype=np.float64)


def _column(rows, column):
    return np.asarray([_to_float(row.get(column)) for row in rows], dtype=np.float64)


def _column_has_finite(rows, column):
    values = _column(rows, column)
    return bool(values.size and np.isfinite(values).any())


def _standardize_design(matrix):
    out = np.asarray(matrix, dtype=np.float64).copy()
    for col_idx in range(1, out.shape[1]):
        col = out[:, col_idx]
        finite = np.isfinite(col)
        if not np.any(finite):
            continue
        mean = float(np.nanmean(col[finite]))
        std = float(np.nanstd(col[finite]))
        if std <= 1e-12:
            out[finite, col_idx] = col[finite] - mean
        else:
            out[finite, col_idx] = (col[finite] - mean) / std
    return out


def _fit_ols(y, x):
    with np.errstate(over="ignore", divide="ignore", invalid="ignore"):
        beta, _, _, _ = np.linalg.lstsq(x, y, rcond=1e-8)
        pred = x @ beta
    if not np.all(np.isfinite(pred)):
        return {"beta": np.full(x.shape[1], np.nan, dtype=np.float64), "r2": np.nan}
    ss_res = float(np.sum((y - pred) ** 2))
    ss_tot = float(np.sum((y - float(np.mean(y))) ** 2))
    r2 = 0.0 if ss_tot <= 1e-12 else 1.0 - ss_res / ss_tot
    return {"beta": beta, "r2": float(max(-np.inf, min(1.0, r2)))}


def _residualize(y, covariates):
    y = np.asarray(y, dtype=np.float64)
    cov = np.asarray(covariates, dtype=np.float64)
    x = _standardize_design(np.column_stack([np.ones(y.shape[0]), cov]))
    model = _fit_ols(y, x)
    beta = model["beta"]
    if not np.all(np.isfinite(beta)):
        return np.full(y.shape, np.nan, dtype=np.float64)
    with np.errstate(over="ignore", divide="ignore", invalid="ignore"):
        residual = y - x @ beta
    if not np.all(np.isfinite(residual)):
        return np.full(y.shape, np.nan, dtype=np.float64)
    return residual


def _pearson_r(x, y):
    x = np.asarray(x, dtype=np.float64)
    y = np.asarray(y, dtype=np.float64)
    valid = np.isfinite(x) & np.isfinite(y)
    if int(np.sum(valid)) < 3:
        return np.nan
    xv = x[valid] - float(np.mean(x[valid]))
    yv = y[valid] - float(np.mean(y[valid]))
    denom = float(np.sqrt(np.sum(xv ** 2) * np.sum(yv ** 2)))
    if denom <= 1e-12:
        return np.nan
    return float(np.sum(xv * yv) / denom)


def _permute_by_day_blocks(values, day_ids, rng):
    arr = np.asarray(values)
    out = np.array(arr, copy=True)
    day_ids = np.asarray(day_ids)
    valid_days = [day for day in np.unique(day_ids[np.isfinite(day_ids)])]
    if len(valid_days) < 2:
        return out
    blocks = [np.flatnonzero(day_ids == day) for day in valid_days]
    permuted = list(rng.permutation(len(blocks)))
    concatenated = np.concatenate([arr[blocks[idx]] for idx in permuted], axis=0)
    finite_order = np.concatenate(blocks, axis=0)
    out[finite_order] = concatenated[:finite_order.shape[0]]
    return out


def _normalize_hourly_feature_table(hourly_feature, hourly_count):
    feature = np.asarray(hourly_feature, dtype=np.float64)
    count = np.asarray(hourly_count, dtype=np.float64)
    weighted_sum = np.nansum(feature * count, axis=0)
    total_count = np.sum(count, axis=0)
    reference = np.divide(
        weighted_sum,
        total_count,
        out=np.full(weighted_sum.shape, np.nan, dtype=np.float64),
        where=total_count > 0,
    )
    return np.divide(
        feature,
        reference[np.newaxis, ...],
        out=np.full_like(feature, np.nan, dtype=np.float64),
        where=np.isfinite(reference[np.newaxis, ...]) & (reference[np.newaxis, ...] > 0),
    )


def _normalize_state_stratified_table(state_stratified, state_count):
    values = np.asarray(state_stratified, dtype=np.float64)
    count = np.asarray(state_count, dtype=np.float64)
    weighted_sum = np.nansum(values * count, axis=0)
    total_count = np.sum(count, axis=0)
    reference = np.divide(
        weighted_sum,
        total_count,
        out=np.full(weighted_sum.shape, np.nan, dtype=np.float64),
        where=total_count > 0,
    )
    return np.divide(
        values,
        reference[np.newaxis, ...],
        out=np.full_like(values, np.nan, dtype=np.float64),
        where=np.isfinite(reference[np.newaxis, ...]) & (reference[np.newaxis, ...] > 0),
    )


def _group_band_values(hourly_feature, group_defs):
    feature = np.asarray(hourly_feature, dtype=np.float64)
    if feature.ndim == 2:
        feature = feature[:, np.newaxis, :]
    n_hours, _, n_bands = feature.shape
    out = np.full((n_hours, len(group_defs), n_bands), np.nan, dtype=np.float64)
    for group_idx, group in enumerate(group_defs):
        out[:, group_idx, :] = _safe_nanmean(feature[:, group, :], axis=1)
    return out


def _state_group_band_values(state_feature, group_defs):
    if state_feature is None:
        return None
    feature = np.asarray(state_feature, dtype=np.float64)
    if feature.ndim != 4:
        return None
    n_hours, n_states, _, n_bands = feature.shape
    out = np.full((n_hours, n_states, len(group_defs), n_bands), np.nan, dtype=np.float64)
    for group_idx, group in enumerate(group_defs):
        out[:, :, group_idx, :] = _safe_nanmean(feature[:, :, group, :], axis=2)
    return out


def _plot_effects(effect_rows, path):
    rows = list(effect_rows or [])[:25]
    fig, ax = plt.subplots(figsize=(12, max(4, 0.36 * max(1, len(rows)))), dpi=140)
    if rows:
        labels = [
            f"{row.get('target', '')}\n{row.get('test_family', '')} q={_format_float(row.get('fdr_q'))}"
            for row in rows
        ][::-1]
        values = [_to_float(row.get("delta_r2")) for row in rows][::-1]
        ax.barh(np.arange(len(rows)), values, color="#4477aa")
        ax.set_yticks(np.arange(len(rows)))
        ax.set_yticklabels(labels, fontsize=7)
    ax.set_xlabel("Delta R2 vs circadian/state baseline")
    ax.set_title("Slice-Level Behavior-Modulated LFP Targets")
    ax.grid(True, axis="x", alpha=0.25)
    fig.tight_layout()
    fig.savefig(path, bbox_inches="tight")
    plt.close(fig)


def _plot_circadian_heatmap(table, effect_rows, path):
    rows = table.get("rows", [])
    target = effect_rows[0]["target"] if effect_rows else _first_target(table)
    fig, ax = plt.subplots(figsize=(12, 5), dpi=140)
    if rows and target:
        max_day = int(max(row["day_index"] for row in rows))
        grid = np.full((max_day + 1, 24), np.nan, dtype=np.float64)
        count = np.zeros((max_day + 1, 24), dtype=np.int32)
        for row in rows:
            day = int(row["day_index"])
            hour = int(np.floor(float(row["zt_hour"]))) % 24
            value = _to_float(row.get(target))
            if np.isfinite(value):
                if np.isnan(grid[day, hour]):
                    grid[day, hour] = 0.0
                grid[day, hour] += value
                count[day, hour] += 1
        grid = np.divide(grid, count, out=np.full_like(grid, np.nan), where=count > 0)
        im = ax.imshow(grid, aspect="auto", interpolation="nearest", cmap="viridis")
        fig.colorbar(im, ax=ax, shrink=0.8)
        ax.set_title(f"Day x ZT Heatmap | {target}")
    else:
        ax.set_title("Day x ZT Heatmap | no LFP target available")
    ax.set_xlabel("ZT hour")
    ax.set_ylabel("Elapsed day")
    ax.set_xticks(np.arange(0, 24, 2))
    fig.tight_layout()
    fig.savefig(path, bbox_inches="tight")
    plt.close(fig)


def _plot_behavior_lfp_overlay(table, effect_rows, path):
    rows = table.get("rows", [])
    target = effect_rows[0]["target"] if effect_rows else _first_target(table)
    fig, axes = plt.subplots(4, 1, figsize=(14, 7), dpi=140, sharex=True)
    if rows:
        x = np.asarray([row["elapsed_day"] for row in rows], dtype=np.float64)
        if target:
            axes[0].plot(x, [_to_float(row.get(target)) for row in rows], color="#1f77b4", lw=1.5, label=f"LFP: {target}")
            axes[0].set_ylabel("LFP")
            axes[0].set_title(target)
        axes[1].step(x, [_to_float(row.get("rule_code")) for row in rows], where="mid", color="#4c78a8", label="Rule code")
        axes[1].set_ylabel("Rule")
        axes[2].plot(x, [_to_float(row.get("perf_pct")) for row in rows], color="#1b7837", label="Performance %")
        axes[2].step(
            x,
            [_to_float(row.get("perf_state_code")) * 35.0 for row in rows],
            where="mid",
            color="#7fbf7b",
            alpha=0.45,
            label="Performance state",
        )
        axes[2].set_ylabel("Perf")
        axes[3].plot(x, [_to_float(row.get("bias_score")) for row in rows], color="#762a83", label="Bias score")
        axes[3].step(
            x,
            [_to_float(row.get("bias_state_code")) - 1.0 for row in rows],
            where="mid",
            color="#b45ac9",
            alpha=0.45,
            label="Bias state",
        )
        axes[3].set_ylabel("Bias")
        axes[3].set_xlabel("Elapsed day")
    for ax in axes:
        ax.grid(True, alpha=0.25)
        if ax.get_legend_handles_labels()[0]:
            ax.legend(loc="upper right", fontsize=8, frameon=False)
    fig.tight_layout()
    fig.savefig(path, bbox_inches="tight")
    plt.close(fig)


def _plot_state_specific_lfp(summary_rows, path):
    rows = list(summary_rows or [])[:35]
    fig, ax = plt.subplots(figsize=(13, max(4, 0.36 * max(1, len(rows)))), dpi=140)
    if rows:
        labels = [
            f"{row.get('channel_group', '')} {row.get('band', '')}\n"
            f"{row.get('test_family', '')} | {row.get('top_state', '')} q={_format_float(row.get('top_state_q'))}"
            for row in rows
        ][::-1]
        values = [_to_float(row.get("top_state_delta_r2")) for row in rows][::-1]
        colors = ["#44aa99" if str(row.get("interpretation", "")).startswith("state_specific") else "#88ccee" for row in rows][::-1]
        ax.barh(np.arange(len(rows)), values, color=colors)
        ax.set_yticks(np.arange(len(rows)))
        ax.set_yticklabels(labels, fontsize=7)
    ax.set_xlabel("Top state Delta R2")
    ax.set_title("State-Specific LFP Behavior Modulation")
    ax.grid(True, axis="x", alpha=0.25)
    fig.tight_layout()
    fig.savefig(path, bbox_inches="tight")
    plt.close(fig)


def _plot_state_occupancy_effects(occupancy_rows, path):
    rows = list(occupancy_rows or [])[:30]
    fig, ax = plt.subplots(figsize=(12, max(4, 0.34 * max(1, len(rows)))), dpi=140)
    if rows:
        labels = [
            f"{row.get('natural_state', '')}\n{row.get('test_family', '')} q={_format_float(row.get('fdr_q'))}"
            for row in rows
        ][::-1]
        values = [_to_float(row.get("delta_r2")) for row in rows][::-1]
        ax.barh(np.arange(len(rows)), values, color="#cc6677")
        ax.set_yticks(np.arange(len(rows)))
        ax.set_yticklabels(labels, fontsize=7)
    ax.set_xlabel("Delta R2 vs circadian/time baseline")
    ax.set_title("Behavior Modulation of Natural-State Occupancy")
    ax.grid(True, axis="x", alpha=0.25)
    fig.tight_layout()
    fig.savefig(path, bbox_inches="tight")
    plt.close(fig)


def _first_target(table):
    targets = table.get("target_columns", [])
    return targets[0] if targets else ""


def _write_csv(rows, path):
    rows = list(rows or [])
    with open(path, "w", encoding="utf-8", newline="") as handle:
        if not rows:
            handle.write("")
            return path
        columns = list(rows[0].keys())
        writer = csv.DictWriter(handle, fieldnames=columns)
        writer.writeheader()
        for row in rows:
            writer.writerow({column: row.get(column, "") for column in columns})
    return path


def _empty_models():
    return {
        "effect_summary": [],
        "term_tests": [],
        "lfp_behavior_modulation": [],
        "state_specific_lfp_summary": [],
        "partial_correlations": [],
        "interaction_tests": [],
        "state_occupancy_tests": [],
        "secondary_checks": [],
        "base_columns": [],
        "n_days": 0,
        "low_confidence": True,
        "n_permutations": 0,
    }


def _normalize_channel_groups(groups, names, n_channels):
    normalized_groups = []
    normalized_names = []
    for idx, group in enumerate(groups or []):
        if not isinstance(group, (list, tuple)):
            continue
        valid = []
        for value in group:
            try:
                ch = int(value)
            except Exception:
                continue
            if 0 <= ch < n_channels and ch not in valid:
                valid.append(ch)
        if valid:
            normalized_groups.append(valid)
            normalized_names.append(str(names[idx]).strip() if idx < len(names or []) and str(names[idx]).strip() else f"group_{idx}")
    if not normalized_groups:
        normalized_groups = [list(range(n_channels))]
        normalized_names = ["All channels"]
    return normalized_groups, normalized_names


def _safe_nanmean(values, axis):
    values = np.asarray(values, dtype=np.float64)
    valid_count = np.sum(np.isfinite(values), axis=axis)
    summed = np.nansum(values, axis=axis)
    return np.divide(
        summed,
        valid_count,
        out=np.full(summed.shape, np.nan, dtype=np.float64),
        where=valid_count > 0,
    )


def _read_string_dataset(dataset):
    return [str(value) for value in dataset[:].astype(str)]


def _fit_length(values, n, default):
    values = np.asarray(values)
    if values.shape[0] == n:
        return values
    out = np.array(default, copy=True)
    take = min(n, values.shape[0])
    if take > 0:
        out[:take] = values[:take]
    return out


def _is_dark_hour(zt_hour, cfg):
    light_on = float(cfg.get("general.light_on_hour", 6.0))
    light_off = float(cfg.get("general.light_off_hour", 18.0))
    if light_on < light_off:
        return zt_hour < light_on or zt_hour >= light_off
    return light_off <= zt_hour < light_on


def _slice_id_from_meta(meta, paths):
    label = meta.get("parent_slice_label") or meta.get("slice_label") or "slice"
    slice_id = meta.get("parent_slice_id") or ""
    source = meta.get("source_root") or " ".join(paths or [])
    match = re.search(r"(BW\d+)", source, re.IGNORECASE)
    bw_id = match.group(1).upper() if match else "BW"
    parts = [bw_id]
    if slice_id:
        parts.append(f"slice_{slice_id}")
    parts.append(label)
    return _sanitize_filename_component("_".join(parts))


def _sanitize_filename_component(value):
    text = str(value or "slice")
    keep = []
    for char in text:
        keep.append(char if char.isalnum() or char in {"-", "_"} else "_")
    cleaned = "_".join(part for part in "".join(keep).split("_") if part)
    return cleaned[:160] or "slice"


def _token(text):
    value = str(text or "unknown").strip().lower()
    keep = []
    for char in value:
        keep.append(char if char.isalnum() else "_")
    token = "_".join(part for part in "".join(keep).split("_") if part)
    return token or "unknown"


def _safe_float(value):
    value = _to_float(value)
    return float(value) if np.isfinite(value) else np.nan


def _to_float(value):
    try:
        if value is None:
            return np.nan
        return float(value)
    except Exception:
        return np.nan


def _format_float(value):
    value = _to_float(value)
    if not np.isfinite(value):
        return "NA"
    return f"{value:.3g}"
