import sys
import os
from PyQt6.QtWidgets import QApplication

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.abspath(os.path.join(BASE_DIR, os.pardir))
for path in (PROJECT_ROOT, BASE_DIR):
    if path not in sys.path:
        sys.path.insert(0, path)

from data_processing import DataProcessor
from verification import VerificationAnalyzer
from gui import MainWindow

if __name__ == '__main__':
    app = QApplication(sys.argv)
    
    # Initialize Core Modules
    dp = DataProcessor()
    verifier = VerificationAnalyzer(dp)
    
    # Start GUI
    window = MainWindow(dp, verifier)
    window.show()
    
    sys.exit(app.exec())
