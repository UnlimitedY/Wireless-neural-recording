import importlib as _importlib
import multiprocessing as _mp
import sys as _sys
from pathlib import Path as _Path


def _load_impl():
    package_name = __package__ or "neural_recorder_GUI"
    package_root = _Path(__file__).resolve().parent
    repo_root = str(package_root.parent)
    if repo_root not in _sys.path:
        _sys.path.insert(0, repo_root)
    return _importlib.import_module(f"{package_name}.recorder_app.window")


_impl = _load_impl()

if __name__ == "__main__":
    _mp.freeze_support()
    _impl.main()
else:
    _sys.modules[__name__] = _impl
