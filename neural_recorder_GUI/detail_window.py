import sys as _sys

try:
    from .master_app import detail_view as _impl
except ImportError:
    from master_app import detail_view as _impl

_sys.modules[__name__] = _impl
