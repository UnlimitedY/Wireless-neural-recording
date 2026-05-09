import multiprocessing
import os
import sys


def main():
    base_dir = os.path.dirname(os.path.abspath(__file__))
    project_root = os.path.abspath(os.path.join(base_dir, os.pardir))
    if project_root not in sys.path:
        sys.path.insert(0, project_root)
    if base_dir not in sys.path:
        sys.path.insert(0, base_dir)

    config_path = os.path.join(base_dir, "config_default.yaml")
    try:
        from continuous_lfp_rhythm.gui_viewer import launch_gui
    except ImportError as exc:
        raise ImportError(
            "Failed to launch the unified GUI. Please install PyQt6 and pyqtgraph first."
        ) from exc

    return launch_gui(default_config=config_path)


if __name__ == "__main__":
    multiprocessing.freeze_support()
    sys.exit(main())
