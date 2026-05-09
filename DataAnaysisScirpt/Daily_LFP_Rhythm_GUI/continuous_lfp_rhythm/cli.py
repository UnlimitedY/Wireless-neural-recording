import argparse
import multiprocessing
import os

from .config import resolve_config
from .features import extract_features
from .file_index import generate_file_coverage, index_files
from .h5_schema import build_daily_h5
from .longitudinal_dynamics import run_longitudinal_dynamics_analysis
from .reports import generate_html_report
from .rhythm_stats import summarize_rhythm
from .state_classification import classify_states
from .timeline import group_by_day


def _load_config(path):
    return resolve_config(path)


def cmd_index_build(args):
    cfg = _load_config(args.config)
    output_dir = args.output_dir or cfg.get("general.output_dir", "") or os.getcwd()
    df = index_files(mode0_dir=args.mode0_dir, mode3_dir=args.mode3_dir)
    if not df:
        raise ValueError("No EDF files found under the provided directories.")
    df = generate_file_coverage(df)
    daily_groups = group_by_day(df, timezone=cfg.get("general.timezone", "Asia/Shanghai"))
    outputs = []
    for date_str, rows in daily_groups.items():
        outputs.append(build_daily_h5(date_str, rows, output_dir, config=cfg))
    return outputs


def cmd_extract_features(args):
    cfg = _load_config(args.config)
    return extract_features(args.h5, cfg)


def cmd_classify_states(args):
    cfg = _load_config(args.config)
    return classify_states(args.h5, cfg)


def cmd_summarize_rhythm(args):
    cfg = _load_config(args.config)
    return summarize_rhythm(args.h5, cfg)


def cmd_report_day(args):
    cfg = _load_config(args.config)
    return generate_html_report(args.h5, output_html=args.output_html, config=cfg)


def cmd_longitudinal_dynamics(args):
    cfg = _load_config(args.config)
    return run_longitudinal_dynamics_analysis(
        args.h5,
        output_dir=args.output_dir,
        config=cfg,
        write_h5=not args.no_write_h5,
        make_figures=not args.no_figures,
    )


def cmd_gui(args):
    try:
        from .gui_viewer import launch_gui
    except ImportError as exc:
        raise ImportError(
            "Launching the unified GUI requires PyQt6 and pyqtgraph to be installed."
        ) from exc
    return launch_gui(default_h5_file=args.h5, default_config=args.config)


def build_parser():
    parser = argparse.ArgumentParser(description="Continuous 24/7 LFP rhythm analysis pipeline")
    subparsers = parser.add_subparsers(dest="command", required=True)

    p_index = subparsers.add_parser("index-build", help="Scan EDF files and build daily H5 files")
    p_index.add_argument("--mode0-dir", default=None)
    p_index.add_argument("--mode3-dir", default=None)
    p_index.add_argument("--output-dir", default=None)
    p_index.add_argument("--config", default=None)
    p_index.set_defaults(func=cmd_index_build)

    p_feat = subparsers.add_parser("extract-features", help="Extract LFP and IMU features from a daily H5")
    p_feat.add_argument("--h5", required=True)
    p_feat.add_argument("--config", default=None)
    p_feat.set_defaults(func=cmd_extract_features)

    p_state = subparsers.add_parser("classify-states", help="Generate working and state labels")
    p_state.add_argument("--h5", required=True)
    p_state.add_argument("--config", default=None)
    p_state.set_defaults(func=cmd_classify_states)

    p_rhythm = subparsers.add_parser("summarize-rhythm", help="Aggregate hourly rhythm statistics")
    p_rhythm.add_argument("--h5", required=True)
    p_rhythm.add_argument("--config", default=None)
    p_rhythm.set_defaults(func=cmd_summarize_rhythm)

    p_report = subparsers.add_parser("report-day", help="Generate an HTML report from a daily H5")
    p_report.add_argument("--h5", required=True)
    p_report.add_argument("--output-html", default=None)
    p_report.add_argument("--config", default=None)
    p_report.set_defaults(func=cmd_report_day)

    p_long = subparsers.add_parser(
        "longitudinal-dynamics",
        help="Build hourly research table and behavior-linked LFP model summaries",
    )
    p_long.add_argument("--h5", required=True)
    p_long.add_argument("--output-dir", default=None)
    p_long.add_argument("--config", default=None)
    p_long.add_argument("--no-write-h5", action="store_true")
    p_long.add_argument("--no-figures", action="store_true")
    p_long.set_defaults(func=cmd_longitudinal_dynamics)

    p_gui = subparsers.add_parser("gui", help="Launch the unified GUI workflow")
    p_gui.add_argument("--h5", default=None)
    p_gui.add_argument("--config", default=None)
    p_gui.set_defaults(func=cmd_gui)

    return parser


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    result = args.func(args)
    if args.command == "gui":
        return result
    if isinstance(result, list):
        for item in result:
            print(item)
    elif result is not None:
        print(result)
    return result


if __name__ == "__main__":
    multiprocessing.freeze_support()
    main()
