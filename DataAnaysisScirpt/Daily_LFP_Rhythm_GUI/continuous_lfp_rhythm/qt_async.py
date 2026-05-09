from PyQt6.QtCore import QEventLoop, QObject, QThread, pyqtSignal, pyqtSlot


class _CallableWorker(QObject):
    finished = pyqtSignal()
    result = pyqtSignal(object)
    error = pyqtSignal(object)

    def __init__(self, func):
        super().__init__()
        self._func = func

    @pyqtSlot()
    def run(self):
        try:
            value = self._func()
        except Exception as exc:  # pragma: no cover - GUI runtime path
            self.error.emit(exc)
        else:
            self.result.emit(value)
        finally:
            self.finished.emit()


def run_callable_in_thread(func):
    loop = QEventLoop()
    thread = QThread()
    worker = _CallableWorker(func)
    worker.moveToThread(thread)

    state = {"result": None, "error": None}

    def _on_result(value):
        state["result"] = value

    def _on_error(exc):
        state["error"] = exc

    worker.result.connect(_on_result)
    worker.error.connect(_on_error)
    thread.started.connect(worker.run)
    worker.finished.connect(loop.quit)
    worker.finished.connect(thread.quit)
    worker.finished.connect(worker.deleteLater)
    thread.finished.connect(thread.deleteLater)

    thread.start()
    loop.exec()
    thread.wait()

    if state["error"] is not None:
        raise state["error"]
    return state["result"]
