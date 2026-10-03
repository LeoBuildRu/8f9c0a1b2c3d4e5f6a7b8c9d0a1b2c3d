"""Тема приложения — Finam-style редизайн (v2, чистая сетка)."""

from PyQt5.QtGui import QPalette, QColor

# Палитра
COLORS = {
    # Поверхности
    "bg":           "#F4F6F9",   # фон страницы
    "panel":        "#FFFFFF",   # карточки
    "panel_alt":    "#F7F9FC",   # вторичные блоки / hover-фоны
    "panel_hover":  "#EEF2F7",
    "header_line":  "#E5E9EF",

    # Текст
    "text":         "#1F2937",
    "text_strong":  "#0B1220",
    "muted":        "#6B7280",
    "muted_2":      "#9CA3AF",

    # Линии / инпуты
    "border":       "#E5E9EF",
    "border_strong":"#D6DBE3",
    "input":        "#FFFFFF",

    # Бренд
    "accent":       "#FFCC00",
    "accent_hover": "#FFD633",
    "accent_press": "#E6B800",
    "accent_ink":   "#1F2937",

    # Состояния рынка
    "success":      "#16A34A",
    "success_bg":   "#E8F6EE",
    "danger":       "#DC2626",
    "danger_bg":    "#FCEBEB",
    "info":         "#2563EB",

    # Биржевой график
    "chart_bg":     "#0E1117",
    "chart_grid":   "#1E2330",
    "chart_text":   "#D1D5DB",
}

# Минимальный глобальный стайлшит — карточки и большинство визуальных решений
# заданы локально в самих компонентах. Глобально только базовые элементы.
APP_STYLESHEET = f"""
QMainWindow, QWidget {{
    background-color: {COLORS['bg']};
    color: {COLORS['text']};
    font-family: 'Inter', 'Segoe UI', 'Noto Sans', sans-serif;
    font-size: 13px;
}}

QToolTip {{
    background-color: {COLORS['text_strong']};
    color: #FFFFFF;
    border: none;
    padding: 6px 10px;
    border-radius: 6px;
    font-size: 12px;
}}

QLabel {{
    background: transparent;
    color: {COLORS['text']};
    border: none;
}}

/* ==== Кнопки ==== */
QPushButton {{
    background: {COLORS['panel']};
    color: {COLORS['text']};
    border: 1px solid {COLORS['border']};
    border-radius: 8px;
    padding: 7px 14px;
    font-weight: 500;
}}
QPushButton:hover {{
    background: {COLORS['panel_hover']};
    color: {COLORS['text_strong']};
}}
QPushButton:pressed {{
    background: {COLORS['panel_alt']};
}}
QPushButton:disabled {{
    background: {COLORS['panel_alt']};
    color: {COLORS['muted_2']};
    border: 1px solid {COLORS['border']};
}}

/* Жёлтая акцентная */
QPushButton[accent="true"] {{
    background: {COLORS['accent']};
    color: {COLORS['accent_ink']};
    border: 1px solid {COLORS['accent']};
    font-weight: 600;
}}
QPushButton[accent="true"]:hover {{
    background: {COLORS['accent_hover']};
    border: 1px solid {COLORS['accent_hover']};
}}
QPushButton[accent="true"]:pressed {{
    background: {COLORS['accent_press']};
    border: 1px solid {COLORS['accent_press']};
}}

/* Призрачные (текстовые) */
QPushButton[ghost="true"] {{
    background: transparent;
    border: none;
    color: {COLORS['muted']};
    padding: 6px 10px;
}}
QPushButton[ghost="true"]:hover {{
    background: {COLORS['panel_alt']};
    color: {COLORS['text_strong']};
}}

/* ==== Поля ввода ==== */
QComboBox, QLineEdit, QTextEdit {{
    background: {COLORS['input']};
    border: 1px solid {COLORS['border']};
    border-radius: 8px;
    color: {COLORS['text_strong']};
    padding: 6px 10px;
    selection-background-color: {COLORS['accent']};
    selection-color: {COLORS['accent_ink']};
}}
QComboBox:hover, QLineEdit:hover, QTextEdit:hover {{
    border: 1px solid {COLORS['border_strong']};
}}
QComboBox:focus, QLineEdit:focus, QTextEdit:focus {{
    border: 1px solid {COLORS['accent']};
}}

QComboBox::drop-down {{
    border: none;
    width: 22px;
}}
QComboBox::down-arrow {{
    image: none;
    border-left: 4px solid transparent;
    border-right: 4px solid transparent;
    border-top: 5px solid {COLORS['muted']};
    margin-right: 8px;
}}
QComboBox QAbstractItemView {{
    background: {COLORS['panel']};
    border: 1px solid {COLORS['border']};
    border-radius: 8px;
    color: {COLORS['text_strong']};
    selection-background-color: {COLORS['accent']};
    selection-color: {COLORS['accent_ink']};
    padding: 4px;
    outline: 0;
}}

/* ==== Скроллбары ==== */
QScrollBar:vertical {{
    background: transparent;
    width: 10px;
    margin: 4px;
    border-radius: 5px;
}}
QScrollBar::handle:vertical {{
    background: {COLORS['border_strong']};
    min-height: 30px;
    border-radius: 5px;
}}
QScrollBar::handle:vertical:hover {{
    background: {COLORS['muted_2']};
}}
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{
    background: transparent; height: 0;
}}
QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical {{
    background: transparent;
}}

QScrollBar:horizontal {{
    background: transparent;
    height: 10px;
    margin: 4px;
    border-radius: 5px;
}}
QScrollBar::handle:horizontal {{
    background: {COLORS['border_strong']};
    min-width: 30px;
    border-radius: 5px;
}}

/* ==== Прогрессбар ==== */
QProgressBar {{
    border: none;
    border-radius: 4px;
    background: {COLORS['panel_alt']};
    text-align: center;
    color: {COLORS['text']};
    height: 6px;
}}
QProgressBar::chunk {{
    background: {COLORS['accent']};
    border-radius: 4px;
}}
"""


def build_palette():
    palette = QPalette()
    palette.setColor(QPalette.Window, QColor(COLORS['bg']))
    palette.setColor(QPalette.WindowText, QColor(COLORS['text']))
    palette.setColor(QPalette.Base, QColor(COLORS['input']))
    palette.setColor(QPalette.AlternateBase, QColor(COLORS['panel_alt']))
    palette.setColor(QPalette.ToolTipBase, QColor(COLORS['text_strong']))
    palette.setColor(QPalette.ToolTipText, QColor("#FFFFFF"))
    palette.setColor(QPalette.Text, QColor(COLORS['text_strong']))
    palette.setColor(QPalette.Button, QColor(COLORS['panel']))
    palette.setColor(QPalette.ButtonText, QColor(COLORS['text']))
    palette.setColor(QPalette.BrightText, QColor(COLORS['danger']))
    palette.setColor(QPalette.Highlight, QColor(COLORS['accent']))
    palette.setColor(QPalette.HighlightedText, QColor(COLORS['accent_ink']))
    return palette
