import os
import sys

from PyQt6.QtWidgets import QApplication

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.abspath(os.path.join(BASE_DIR, os.pardir))
for path in (PROJECT_ROOT, BASE_DIR):
    if path not in sys.path:
        sys.path.insert(0, path)

from gui import RawDataOverviewWindow


if __name__ == "__main__":
    app = QApplication(sys.argv)
    window = RawDataOverviewWindow()
    window.show()
    sys.exit(app.exec())
