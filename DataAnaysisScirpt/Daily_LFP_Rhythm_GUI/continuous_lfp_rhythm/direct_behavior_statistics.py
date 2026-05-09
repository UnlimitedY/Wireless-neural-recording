import csv
import json
import os
import tempfile

os.environ.setdefault("MPLCONFIGDIR", os.path.join(tempfile.gettempdir(), "matplotlib"))
os.environ.setdefault("MPLBACKEND", "Agg")

import numpy as np
import matplotlib

matplotlib.use("Agg")
from matplotlib import pyplot as plt

from Common_Analysis.behavior_analysis import BIAS_STATE_LABELS, PERF_STATE_LABELS, RULE_LABELS
from Common_Analysis.behavior_rt import RT_CLASS_LABELS

from .slice_statistics import (
    _annotate_test_row,
    _assign_fdr_q_values,
    _format_float,
    _sanitize_filename_component,
    _sort_test_rows,
    _to_float,
    build_slice_statistics_table,
    slice_statistics_id_from_h5_paths,
)

try:
    from scipy import stats as _scipy_stats
except Exception:
    _scipy_stats = None


ANALYSIS_VERSION = 1
DEFAULT_N_PERMUTATIONS = 1000
MIN_GROUP_N = 5


def run_direct_behavior_state_statistics(
    h5_paths,
    output_dir,
    config=None,
    reference_scope="daily",
    n_permutations=DEFAULT_N_PERMUTATIONS,
    random_seed=0,
    make_figures=True,
):
    table = build_slice_statistics_table(h5_paths, config=config, reference_scope=reference_scope)
    stats = compute_direct_behavior_state_statistics(
        table,
        n_permutations=n_permutations,
        random_seed=random_seed,
    )
    result = {
        "version": ANALYSIS_VERSION,
        "table": table,
        "stats": stats,
        "reference_scope": str(reference_scope or "daily"),
        "n_permutations": int(n_permutations),
        "random_seed": int(random_seed),
        "make_figures": bool(make_figures),
    }
    slice_id = table.get("slice_id") or slice_statistics_id_from_h5_paths(h5_paths)
    return write_direct_behavior_statistics_outputs(result, output_dir, slice_id, make_figures=make_figures)


def compute_direct_behavior_state_statistics(table, n_permutations=DEFAULT_N_PERMUTATIONS, random_seed=0):
    rows = list(table.get("rows", []))
    rng = np.random.default_rng(int(random_seed))
    lfp_targets = list(table.get("target_columns", []))
    occupancy_targets = list(table.get("state_occupancy_columns", []))
    group_specs = _behavior_group_specs()

    lfp_group_rows = []
    lfp_pairwise_rows = []
    occupancy_group_rows = []
    for target in lfp_targets:
        for spec in group_specs:
            group_row, pair_rows = _direct_group_test(
                rows,
                target,
                spec,
                table,
                rng,
                int(n_permutations),
                analysis_family="direct_lfp_behavior_group_difference",
            )
            lfp_group_rows.append(group_row)
            lfp_pairwise_rows.extend(pair_rows)

    for target in occupancy_targets:
        for spec in group_specs:
            group_row, _ = _direct_group_test(
                rows,
                target,
                spec,
                table,
                rng,
                int(n_permutations),
                analysis_family="direct_state_occupancy_behavior_group_difference",
            )
            occupancy_group_rows.append(group_row)

    continuous_rows = []
    for target in lfp_targets:
        for term in ["perf_pct", "bias_score", "rt_median_ms"]:
            continuous_rows.append(_direct_spearman_test(rows, target, term, table))

    _assign_fdr_q_values(lfp_group_rows)
    _assign_fdr_q_values(lfp_pairwise_rows)
    _assign_fdr_q_values(occupancy_group_rows)
    _assign_fdr_q_values(continuous_rows)

    lfp_group_rows = _sort_direct_rows(lfp_group_rows)
    lfp_pairwise_rows = _sort_direct_rows(lfp_pairwise_rows)
    occupancy_group_rows = _sort_direct_rows(occupancy_group_rows)
    continuous_rows = _sort_direct_rows(continuous_rows)

    return {
        "lfp_group_tests": lfp_group_rows,
        "lfp_pairwise_tests": lfp_pairwise_rows,
        "state_occupancy_group_tests": occupancy_group_rows,
        "continuous_associations": continuous_rows,
        "n_permutations": int(n_permutations),
        "method": (
            "Direct hourly group comparisons without regression covariates. Neural targets include LFP and any loaded PLV/PAC "
            "connectivity targets. Omnibus p values use label permutation of behavior groups; pairwise p values use Mann-Whitney U "
            "when scipy is available."
        ),
    }


def write_direct_behavior_statistics_outputs(result, output_dir, slice_id, make_figures=True):
    output_dir = os.path.abspath(str(output_dir or os.getcwd()))
    os.makedirs(output_dir, exist_ok=True)
    safe_id = _sanitize_filename_component(slice_id or "slice")
    table = result["table"]
    stats = result["stats"]
    rows = table.get("rows", [])

    hourly_csv = os.path.join(output_dir, f"{safe_id}_direct_behavior_hourly_table.csv")
    group_csv = os.path.join(output_dir, f"{safe_id}_direct_behavior_lfp_group_tests.csv")
    pairwise_csv = os.path.join(output_dir, f"{safe_id}_direct_behavior_lfp_pairwise_tests.csv")
    occupancy_csv = os.path.join(output_dir, f"{safe_id}_direct_behavior_state_occupancy_tests.csv")
    continuous_csv = os.path.join(output_dir, f"{safe_id}_direct_behavior_continuous_associations.csv")
    summary_json = os.path.join(output_dir, f"{safe_id}_direct_behavior_summary.json")

    _write_csv(rows, hourly_csv)
    _write_csv(stats.get("lfp_group_tests", []), group_csv)
    _write_csv(stats.get("lfp_pairwise_tests", []), pairwise_csv)
    _write_csv(stats.get("state_occupancy_group_tests", []), occupancy_csv)
    _write_csv(stats.get("continuous_associations", []), continuous_csv)

    figure_paths = []
    if make_figures:
        figure_paths = make_direct_behavior_statistics_figures(result, output_dir, safe_id)

    summary = {
        "version": ANALYSIS_VERSION,
        "slice_id": safe_id,
        "reference_scope": result.get("reference_scope", table.get("reference_scope", "daily")),
        "n_permutations": int(result.get("n_permutations", stats.get("n_permutations", 0))),
        "random_seed": int(result.get("random_seed", 0)),
        "n_hours": int(len(rows)),
        "n_h5_files": int(len(table.get("h5_paths", []))),
        "h5_paths": list(table.get("h5_paths", [])),
        "hourly_csv": hourly_csv,
        "lfp_group_tests_csv": group_csv,
        "lfp_pairwise_tests_csv": pairwise_csv,
        "state_occupancy_tests_csv": occupancy_csv,
        "continuous_associations_csv": continuous_csv,
        "summary_json": summary_json,
        "figure_paths": figure_paths,
        "top_lfp_group_differences": stats.get("lfp_group_tests", [])[:40],
        "top_lfp_pairwise_differences": stats.get("lfp_pairwise_tests", [])[:40],
        "top_state_occupancy_differences": stats.get("state_occupancy_group_tests", [])[:30],
        "top_continuous_associations": stats.get("continuous_associations", [])[:30],
        "method": stats.get("method", ""),
        "warnings": list(table.get("warnings", [])),
        "interpretation_note": (
            "This panel is intentionally descriptive and non-regression-based: it asks whether hourly neural feature "
            "distributions differ across behavior states. It does not control circadian phase, elapsed day, "
            "or state occupancy, so use it as raw evidence alongside the model-based Slice Statistics panel."
        ),
    }
    with open(summary_json, "w", encoding="utf-8") as handle:
        json.dump(summary, handle, ensure_ascii=False, indent=2)
    return summary


def make_direct_behavior_statistics_figures(result, output_dir, slice_id):
    table = result["table"]
    stats = result["stats"]
    paths = []
    effects_path = os.path.join(output_dir, f"{slice_id}_direct_behavior_lfp_effects.png")
    distribution_path = os.path.join(output_dir, f"{slice_id}_direct_behavior_lfp_distributions.png")
    occupancy_path = os.path.join(output_dir, f"{slice_id}_direct_behavior_state_occupancy_effects.png")
    _plot_direct_effects(stats.get("lfp_group_tests", []), effects_path)
    _plot_direct_distributions(table, stats.get("lfp_group_tests", []), distribution_path)
    _plot_direct_effects(stats.get("state_occupancy_group_tests", []), occupancy_path, title="Direct Behavior Differences in State Occupancy")
    paths.extend([effects_path, distribution_path, occupancy_path])
    return paths


def expected_direct_behavior_statistics_summary_path(h5_paths, output_dir):
    slice_id = slice_statistics_id_from_h5_paths(h5_paths)
    return os.path.join(
        os.path.abspath(str(output_dir or os.getcwd())),
        f"{_sanitize_filename_component(slice_id)}_direct_behavior_summary.json",
    )


def _behavior_group_specs():
    return [
        {
            "column": "rule_code",
            "family": "rule_group",
            "label": "Rule",
            "labels": RULE_LABELS,
            "valid": {0, 1, 2},
        },
        {
            "column": "perf_state_code",
            "family": "performance_state_group",
            "label": "Performance state",
            "labels": PERF_STATE_LABELS,
            "valid": {0, 1, 2},
        },
        {
            "column": "bias_state_code",
            "family": "bias_state_group",
            "label": "Bias state",
            "labels": BIAS_STATE_LABELS,
            "valid": {0, 1, 2},
        },
        {
            "column": "rt_class_code",
            "family": "rt_class_group",
            "label": "RT class",
            "labels": RT_CLASS_LABELS,
            "valid": {0, 1, 2},
        },
    ]


def _direct_group_test(rows, target, spec, table, rng, n_permutations, analysis_family):
    y = np.asarray([_to_float(row.get(target)) for row in rows], dtype=np.float64)
    group = np.asarray([_to_float(row.get(spec["column"])) for row in rows], dtype=np.float64)
    valid = np.isfinite(y) & np.isfinite(group)
    valid &= np.asarray([int(value) in spec["valid"] if np.isfinite(value) else False for value in group], dtype=bool)
    yv = y[valid]
    gv = group[valid].astype(np.int16)
    groups = _group_values(yv, gv, spec)
    summaries = _summarize_groups(groups)
    n_obs = int(yv.size)
    n_groups = int(sum(1 for values in groups.values() if len(values) >= MIN_GROUP_N))
    top = _top_group_contrast(groups, spec)
    row = {
        "target": target,
        "test_family": spec["family"],
        "behavior_variable": spec["column"],
        "behavior_label": spec["label"],
        "n_obs": n_obs,
        "n_groups": n_groups,
        "effect_size": np.nan,
        "max_abs_median_diff": np.nan,
        "max_abs_mean_diff": np.nan,
        "eta2": np.nan,
        "permutation_p": np.nan,
        "fdr_q": np.nan,
        "top_contrast": top.get("contrast", ""),
        "top_contrast_label": top.get("contrast_label", ""),
        "top_median_diff": top.get("median_diff", np.nan),
        "top_mean_diff": top.get("mean_diff", np.nan),
        "top_cliffs_delta": top.get("cliffs_delta", np.nan),
        "group_summaries_json": json.dumps(summaries, ensure_ascii=False),
    }
    row = _annotate_test_row(row, table, analysis_family)
    pair_rows = []
    if n_obs < MIN_GROUP_N * 2 or n_groups < 2:
        return row, pair_rows

    stat = _direct_group_statistic(groups)
    row["effect_size"] = float(stat["effect_size"])
    row["max_abs_median_diff"] = float(stat["max_abs_median_diff"])
    row["max_abs_mean_diff"] = float(stat["max_abs_mean_diff"])
    row["eta2"] = float(stat["eta2"])
    row["permutation_p"] = _permutation_group_pvalue(yv, gv, spec, stat["effect_size"], rng, n_permutations)
    pair_rows = [_annotate_test_row(pair, table, analysis_family + "_pairwise") for pair in _pairwise_rows(target, groups, spec)]
    return row, pair_rows


def _direct_spearman_test(rows, target, term, table):
    y = np.asarray([_to_float(row.get(target)) for row in rows], dtype=np.float64)
    x = np.asarray([_to_float(row.get(term)) for row in rows], dtype=np.float64)
    valid = np.isfinite(x) & np.isfinite(y)
    n_obs = int(np.sum(valid))
    rho = np.nan
    p = np.nan
    if n_obs >= 8:
        if _scipy_stats is not None:
            try:
                rho, p = _scipy_stats.spearmanr(x[valid], y[valid], nan_policy="omit")
            except Exception:
                rho, p = np.nan, np.nan
        else:
            rho = _pearson_r(_rankdata(x[valid]), _rankdata(y[valid]))
    row = {
        "target": target,
        "test_family": f"{term}_spearman",
        "behavior_variable": term,
        "behavior_label": _continuous_behavior_label(term),
        "n_obs": n_obs,
        "spearman_r": float(rho) if np.isfinite(rho) else np.nan,
        "effect_size": abs(float(rho)) if np.isfinite(rho) else np.nan,
        "permutation_p": float(p) if np.isfinite(p) else np.nan,
        "fdr_q": np.nan,
        "effect_sign": "positive" if np.isfinite(rho) and rho > 0 else "negative" if np.isfinite(rho) and rho < 0 else "",
    }
    return _annotate_test_row(row, table, "direct_lfp_continuous_behavior_association")


def _continuous_behavior_label(term):
    if term == "perf_pct":
        return "Performance %"
    if term == "bias_score":
        return "Bias score"
    if term == "rt_median_ms":
        return "RT median ms"
    return str(term)


def _group_values(values, groups, spec):
    out = {}
    for code in sorted(spec["valid"]):
        out[int(code)] = np.asarray(values[groups == int(code)], dtype=np.float64)
    return out


def _summarize_groups(groups):
    summaries = {}
    for code, values in groups.items():
        finite = values[np.isfinite(values)]
        if finite.size:
            summaries[str(int(code))] = {
                "n": int(finite.size),
                "mean": float(np.mean(finite)),
                "median": float(np.median(finite)),
                "q25": float(np.percentile(finite, 25)),
                "q75": float(np.percentile(finite, 75)),
            }
        else:
            summaries[str(int(code))] = {"n": 0, "mean": np.nan, "median": np.nan, "q25": np.nan, "q75": np.nan}
    return summaries


def _direct_group_statistic(groups):
    usable = {code: values[np.isfinite(values)] for code, values in groups.items() if np.sum(np.isfinite(values)) >= MIN_GROUP_N}
    if len(usable) < 2:
        return {
            "effect_size": np.nan,
            "max_abs_median_diff": np.nan,
            "max_abs_mean_diff": np.nan,
            "eta2": np.nan,
        }
    all_values = np.concatenate(list(usable.values())) if usable else np.zeros((0,), dtype=np.float64)
    medians = {code: float(np.median(values)) for code, values in usable.items()}
    means = {code: float(np.mean(values)) for code, values in usable.items()}
    max_median = _max_abs_pairwise_diff(medians)
    max_mean = _max_abs_pairwise_diff(means)
    pooled_iqr = _pooled_iqr(list(usable.values()))
    eta2 = _eta_squared(usable, all_values)
    effect_size = max_median / pooled_iqr if pooled_iqr > 1e-12 else max_median
    return {
        "effect_size": float(abs(effect_size)),
        "max_abs_median_diff": float(abs(max_median)),
        "max_abs_mean_diff": float(abs(max_mean)),
        "eta2": float(eta2),
    }


def _top_group_contrast(groups, spec):
    usable = {code: values[np.isfinite(values)] for code, values in groups.items() if np.sum(np.isfinite(values)) >= MIN_GROUP_N}
    best = {"abs_diff": -np.inf}
    codes = sorted(usable.keys())
    for idx, code_a in enumerate(codes):
        for code_b in codes[idx + 1:]:
            a = usable[code_a]
            b = usable[code_b]
            median_diff = float(np.median(b) - np.median(a))
            mean_diff = float(np.mean(b) - np.mean(a))
            abs_diff = abs(median_diff)
            if abs_diff > best["abs_diff"]:
                best = {
                    "abs_diff": abs_diff,
                    "contrast": f"{int(code_b)}-vs-{int(code_a)}",
                    "contrast_label": f"{_group_label(spec, code_b)} vs {_group_label(spec, code_a)}",
                    "median_diff": median_diff,
                    "mean_diff": mean_diff,
                    "cliffs_delta": _cliffs_delta(b, a),
                }
    if best["abs_diff"] < 0:
        return {}
    return best


def _pairwise_rows(target, groups, spec):
    rows = []
    usable = {code: values[np.isfinite(values)] for code, values in groups.items() if np.sum(np.isfinite(values)) >= MIN_GROUP_N}
    codes = sorted(usable.keys())
    for idx, code_a in enumerate(codes):
        for code_b in codes[idx + 1:]:
            a = usable[code_a]
            b = usable[code_b]
            p = np.nan
            if _scipy_stats is not None and a.size >= MIN_GROUP_N and b.size >= MIN_GROUP_N:
                try:
                    p = float(_scipy_stats.mannwhitneyu(b, a, alternative="two-sided").pvalue)
                except Exception:
                    p = np.nan
            rows.append(
                {
                    "target": target,
                    "test_family": spec["family"] + "_pairwise",
                    "behavior_variable": spec["column"],
                    "behavior_label": spec["label"],
                    "contrast": f"{int(code_b)}-vs-{int(code_a)}",
                    "contrast_label": f"{_group_label(spec, code_b)} vs {_group_label(spec, code_a)}",
                    "n_a": int(a.size),
                    "n_b": int(b.size),
                    "median_a": float(np.median(a)),
                    "median_b": float(np.median(b)),
                    "mean_a": float(np.mean(a)),
                    "mean_b": float(np.mean(b)),
                    "median_diff": float(np.median(b) - np.median(a)),
                    "mean_diff": float(np.mean(b) - np.mean(a)),
                    "effect_size": abs(float(np.median(b) - np.median(a))),
                    "cliffs_delta": _cliffs_delta(b, a),
                    "permutation_p": p,
                    "fdr_q": np.nan,
                }
            )
    return rows


def _permutation_group_pvalue(values, groups, spec, observed, rng, n_permutations):
    if n_permutations <= 0 or not np.isfinite(observed):
        return np.nan
    extreme = 0
    valid_perm = 0
    for _ in range(int(n_permutations)):
        perm_groups = np.array(groups, copy=True)
        rng.shuffle(perm_groups)
        perm_stat = _direct_group_statistic(_group_values(values, perm_groups, spec))["effect_size"]
        if np.isfinite(perm_stat):
            extreme += int(perm_stat >= observed - 1e-12)
            valid_perm += 1
    if valid_perm <= 0:
        return np.nan
    return float((extreme + 1.0) / (valid_perm + 1.0))


def _max_abs_pairwise_diff(values_by_code):
    codes = sorted(values_by_code.keys())
    if len(codes) < 2:
        return np.nan
    best = 0.0
    for idx, code_a in enumerate(codes):
        for code_b in codes[idx + 1:]:
            best = max(best, abs(values_by_code[code_b] - values_by_code[code_a]))
    return float(best)


def _pooled_iqr(groups):
    all_values = np.concatenate([values[np.isfinite(values)] for values in groups if np.any(np.isfinite(values))])
    if all_values.size < 2:
        return np.nan
    iqr = float(np.percentile(all_values, 75) - np.percentile(all_values, 25))
    if iqr <= 1e-12:
        mad = float(np.median(np.abs(all_values - np.median(all_values))))
        return mad if mad > 1e-12 else 1.0
    return iqr


def _eta_squared(groups, all_values):
    if all_values.size < 2:
        return np.nan
    grand = float(np.mean(all_values))
    ss_total = float(np.sum((all_values - grand) ** 2))
    if ss_total <= 1e-12:
        return 0.0
    ss_between = 0.0
    for values in groups.values():
        if values.size:
            ss_between += float(values.size) * (float(np.mean(values)) - grand) ** 2
    return float(max(0.0, min(1.0, ss_between / ss_total)))


def _cliffs_delta(x, y):
    x = np.asarray(x, dtype=np.float64)
    y = np.asarray(y, dtype=np.float64)
    x = x[np.isfinite(x)]
    y = y[np.isfinite(y)]
    if x.size == 0 or y.size == 0:
        return np.nan
    diff = x[:, np.newaxis] - y[np.newaxis, :]
    return float((np.sum(diff > 0) - np.sum(diff < 0)) / float(x.size * y.size))


def _group_label(spec, code):
    return str(spec["labels"].get(int(code), str(int(code))))


def _sort_direct_rows(rows):
    def key(row):
        q = _to_float(row.get("fdr_q"))
        p = _to_float(row.get("permutation_p"))
        effect = _to_float(row.get("effect_size"))
        if not np.isfinite(effect):
            effect = abs(_to_float(row.get("spearman_r")))
        return (
            q if np.isfinite(q) else np.inf,
            p if np.isfinite(p) else np.inf,
            -effect if np.isfinite(effect) else np.inf,
            str(row.get("target", "")),
        )

    return sorted(list(rows or []), key=key)


def _rankdata(values):
    order = np.argsort(values)
    ranks = np.empty(values.shape[0], dtype=np.float64)
    ranks[order] = np.arange(values.shape[0], dtype=np.float64)
    return ranks


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


def _plot_direct_effects(rows, path, title="Direct Behavior Differences in LFP"):
    rows = list(rows or [])[:30]
    fig, ax = plt.subplots(figsize=(12, max(4, 0.36 * max(1, len(rows)))), dpi=140)
    if rows:
        labels = [
            f"{row.get('target', '')}\n{row.get('behavior_label', '')} q={_format_float(row.get('fdr_q'))}"
            for row in rows
        ][::-1]
        values = [_to_float(row.get("effect_size")) for row in rows][::-1]
        ax.barh(np.arange(len(rows)), values, color="#117733")
        ax.set_yticks(np.arange(len(rows)))
        ax.set_yticklabels(labels, fontsize=7)
    ax.set_xlabel("Max median group difference / pooled IQR")
    ax.set_title(title)
    ax.grid(True, axis="x", alpha=0.25)
    fig.tight_layout()
    fig.savefig(path, bbox_inches="tight")
    plt.close(fig)


def _plot_direct_distributions(table, group_rows, path):
    rows = table.get("rows", [])
    top = list(group_rows or [])[:1]
    fig, ax = plt.subplots(figsize=(9, 5), dpi=140)
    if rows and top:
        row = top[0]
        target = row.get("target", "")
        variable = row.get("behavior_variable", "")
        spec = next((item for item in _behavior_group_specs() if item["column"] == variable), None)
        if target and spec:
            values = np.asarray([_to_float(item.get(target)) for item in rows], dtype=np.float64)
            groups = np.asarray([_to_float(item.get(variable)) for item in rows], dtype=np.float64)
            data = []
            labels = []
            for code in sorted(spec["valid"]):
                selected = values[(groups == int(code)) & np.isfinite(values)]
                if selected.size:
                    data.append(selected)
                    labels.append(_group_label(spec, code))
            if data:
                ax.boxplot(data, labels=labels, showfliers=False)
                for idx, selected in enumerate(data, start=1):
                    if selected.size > 300:
                        selected = selected[np.linspace(0, selected.size - 1, 300).astype(int)]
                    jitter = np.linspace(-0.18, 0.18, selected.size) if selected.size > 1 else np.zeros(selected.size)
                    ax.scatter(np.full(selected.size, idx) + jitter, selected, s=8, alpha=0.35)
                ax.set_title(f"{target} grouped by {row.get('behavior_label', '')}")
                ax.set_ylabel("Hourly LFP value")
    else:
        ax.set_title("Direct Behavior Distribution | no target available")
    ax.grid(True, axis="y", alpha=0.25)
    fig.tight_layout()
    fig.savefig(path, bbox_inches="tight")
    plt.close(fig)


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
