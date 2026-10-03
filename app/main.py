"""Точка входа приложения."""

import os
import sys

from PyQt5.QtWidgets import QApplication
from PyQt5.QtGui import QFont

from app.main_window import StockAnalysisApp
from app.theme import APP_STYLESHEET, build_palette


def main():
    os.environ.setdefault("QT_LOGGING_RULES", "qt.qpa.fonts=false")

    app = QApplication(sys.argv)
    app.setStyle('Fusion')
    app.setPalette(build_palette())
    app.setStyleSheet(APP_STYLESHEET)
    app.setFont(QFont("Segoe UI", 10))

    window = StockAnalysisApp()
    window.show()
    sys.exit(app.exec_())
