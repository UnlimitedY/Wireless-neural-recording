import os


def configure_qt_runtime() -> None:
    try:
        from PyQt6.QtCore import Qt
        from PyQt6.QtWidgets import QApplication

        attribute = getattr(Qt.ApplicationAttribute, "AA_CompressHighFrequencyEvents", None)
        if attribute is not None:
            QApplication.setAttribute(attribute, True)
    except Exception:
        pass


def _set_priority_class(priority_class: int) -> None:
    if os.name != "nt":
        return
    try:
        import ctypes

        kernel32 = ctypes.windll.kernel32
        current_process = kernel32.GetCurrentProcess()
        kernel32.SetPriorityClass(current_process, int(priority_class))
    except Exception:
        pass


def set_execution_state(active: bool) -> None:
    if os.name != "nt":
        return
    try:
        import ctypes

        kernel32 = ctypes.windll.kernel32
        ES_CONTINUOUS = 0x80000000
        ES_SYSTEM_REQUIRED = 0x00000001
        if active:
            kernel32.SetThreadExecutionState(ES_CONTINUOUS | ES_SYSTEM_REQUIRED)
        else:
            kernel32.SetThreadExecutionState(ES_CONTINUOUS)
    except Exception:
        pass


def apply_windows_process_role(role: str, active_recording: bool = False) -> None:
    if os.name != "nt":
        return

    NORMAL_PRIORITY_CLASS = 0x00000020
    ABOVE_NORMAL_PRIORITY_CLASS = 0x00008000
    HIGH_PRIORITY_CLASS = 0x00000080
    BELOW_NORMAL_PRIORITY_CLASS = 0x00004000

    normalized_role = str(role or "").strip().lower()
    if normalized_role == "data_writer":
        # EDF writing is background work; acquisition must keep CPU priority.
        _set_priority_class(BELOW_NORMAL_PRIORITY_CLASS)
        return
    if normalized_role == "slot_service":
        priority = ABOVE_NORMAL_PRIORITY_CLASS if active_recording else NORMAL_PRIORITY_CLASS
        _set_priority_class(priority)
        set_execution_state(bool(active_recording))
        return
    if normalized_role == "camera_capture":
        priority = ABOVE_NORMAL_PRIORITY_CLASS if active_recording else NORMAL_PRIORITY_CLASS
        _set_priority_class(priority)
        set_execution_state(bool(active_recording))
        return
    if normalized_role == "video_compress":
        _set_priority_class(BELOW_NORMAL_PRIORITY_CLASS)
        return
    if normalized_role == "detail_view":
        _set_priority_class(BELOW_NORMAL_PRIORITY_CLASS)
        return
    if normalized_role == "master_console":
        _set_priority_class(NORMAL_PRIORITY_CLASS)
