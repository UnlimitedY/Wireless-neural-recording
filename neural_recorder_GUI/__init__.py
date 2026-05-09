"""Package compatibility helpers for the neural recorder GUI stack."""

__all__ = ["ESBMainWindow"]


def __getattr__(name):
    if name == "ESBMainWindow":
        from .recorder_app.window import ESBMainWindow

        return ESBMainWindow
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
