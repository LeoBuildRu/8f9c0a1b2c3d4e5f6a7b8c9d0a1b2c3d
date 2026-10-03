"""Компоненты интерфейса и worker-классы — Finam-style редизайн (v2)."""

from datetime import datetime, timedelta
import requests
import pandas as pd

from PyQt5.QtWidgets import (
    QWidget, QComboBox, QLabel, QFrame, QPushButton,
    QSizePolicy, QProgressBar, QScrollArea, QTextEdit, QLineEdit,
    QVBoxLayout, QHBoxLayout, QGridLayout
)
from PyQt5.QtCore import Qt, QPropertyAnimation, QEasingCurve, pyqtProperty, QTimer, QThread, pyqtSignal
from PyQt5.QtGui import QFont
import matplotlib.pyplot as plt
from matplotlib.backends.backend_qt5agg import FigureCanvasQTAgg as FigureCanvas
from matplotlib.figure import Figure
import matplotlib.dates as mdates
from matplotlib import rcParams
from matplotlib.patches import Rectangle
import xml.etree.ElementTree as ET

from ollama_worker import OllamaWorker, NewsParser
from app.data_services import fetch_stock_data_with_fallback
from app.theme import COLORS


# =============================================================================
# Утилитарные функции
# =============================================================================
def card_style(extra: str = "") -> str:
    """Единый стиль карточки."""
    return f"""
        QFrame#card {{
            background: {COLORS['panel']};
            border: 1px solid {COLORS['border']};
            border-radius: 12px;
        }}
        {extra}
    """


def make_card() -> QFrame:
    """Создаёт пустой QFrame с единым card-стилем."""
    f = QFrame()
    f.setObjectName("card")
    f.setStyleSheet(card_style())
    return f


def section_title(text: str) -> QLabel:
    """Заголовок секции внутри карточки."""
    lbl = QLabel(text)
    lbl.setFont(QFont("Segoe UI", 11, QFont.DemiBold))
    lbl.setStyleSheet(f"color: {COLORS['text_strong']};")
    return lbl


def small_label(text: str) -> QLabel:
    """Маленький uppercase-лейбл с трекингом."""
    lbl = QLabel(text.upper())
    lbl.setFont(QFont("Segoe UI", 8, QFont.DemiBold))
    lbl.setStyleSheet(f"color: {COLORS['muted']}; letter-spacing: 0.7px;")
    return lbl


def vline() -> QFrame:
    s = QFrame()
    s.setFrameShape(QFrame.VLine)
    s.setStyleSheet(f"background: {COLORS['border']}; color: {COLORS['border']};")
    s.setFixedWidth(1)
    return s


def hline() -> QFrame:
    s = QFrame()
    s.setFrameShape(QFrame.HLine)
    s.setStyleSheet(f"background: {COLORS['border']}; color: {COLORS['border']};")
    s.setFixedHeight(1)
    return s


# =============================================================================
# ИИ-АССИСТЕНТ
# =============================================================================
class AIChatWidget(QFrame):
    PERIODS = ["1Д", "1Н", "1М", "3М", "6М", "1Г"]
    PERIOD_TEXTS = {
        "1Д": "конец следующего торгового дня",
        "1Н": "конец следующей недели",
        "1М": "конец следующего месяца",
        "3М": "конец следующего квартала",
        "6М": "через 6 месяцев",
        "1Г": "через год",
    }

    def __init__(self, parent=None):
        super().__init__(parent)
        self.stock_context = ""
        self.chat_history = []
        self.current_worker = None
        self.current_stock = "N/A"
        self.current_stock_data = None
        self.all_stocks_data = {}
        self.market_sentiment = ""
        self.currency_rates = {}

        self.setObjectName("card")
        self.setStyleSheet(card_style())
        self.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Expanding)

        layout = QVBoxLayout()
        layout.setContentsMargins(16, 14, 16, 14)
        layout.setSpacing(10)

        # ── Шапка
        header = QHBoxLayout()
        header.setSpacing(8)
        title = section_title("ИИ-ассистент")
        header.addWidget(title)
        header.addStretch()

        # ── Чат
        self.chat_display = QTextEdit()
        self.chat_display.setReadOnly(True)
        self.chat_display.setFont(QFont("Segoe UI", 9))
        self.chat_display.setMinimumHeight(140)
        self.chat_display.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Expanding)
        self.chat_display.setStyleSheet(f"""
            QTextEdit {{
                background: {COLORS['panel_alt']};
                border: 1px solid {COLORS['border']};
                border-radius: 10px;
                color: {COLORS['text']};
                padding: 10px;
            }}
        """)

        # ── Чипы периодов (короткие ярлыки, автоширина)
        chips_label = small_label("Прогноз для текущей акции")

        chips_row = QHBoxLayout()
        chips_row.setSpacing(6)
        chips_row.setContentsMargins(0, 0, 0, 0)

        self.period_buttons = {}
        for period in self.PERIODS:
            btn = QPushButton(period)
            btn.setFixedHeight(28)
            btn.setMinimumWidth(40)
            btn.setFont(QFont("Segoe UI", 9, QFont.DemiBold))
            btn.setCursor(Qt.PointingHandCursor)
            btn.setStyleSheet(f"""
                QPushButton {{
                    background: {COLORS['panel_alt']};
                    border: 1px solid {COLORS['border']};
                    border-radius: 14px;
                    color: {COLORS['text']};
                    padding: 0 12px;
                }}
                QPushButton:hover {{
                    background: #FFF6CC;
                    border: 1px solid {COLORS['accent']};
                    color: {COLORS['text_strong']};
                }}
                QPushButton:pressed {{
                    background: #FFEFA6;
                }}
            """)
            btn.clicked.connect(lambda checked, p=period: self.quick_period_query(p))
            chips_row.addWidget(btn)
            self.period_buttons[period] = btn
        chips_row.addStretch()

        # ── Кнопка «по всему рынку»
        all_btn = QPushButton("Прогноз на завтра по всем акциям")
        all_btn.setFixedHeight(34)
        all_btn.setFont(QFont("Segoe UI", 9, QFont.DemiBold))
        all_btn.setCursor(Qt.PointingHandCursor)
        all_btn.setProperty("accent", True)
        all_btn.style().unpolish(all_btn)
        all_btn.style().polish(all_btn)
        all_btn.clicked.connect(self.quick_all_stocks_query)

        # ── Поле ввода + Send
        input_row = QHBoxLayout()
        input_row.setSpacing(8)

        self.input_field = QLineEdit()
        self.input_field.setFont(QFont("Segoe UI", 10))
        self.input_field.setPlaceholderText("Задайте вопрос о рынке или акциях…")
        self.input_field.setFixedHeight(34)
        self.input_field.returnPressed.connect(self.send_message)

        self.send_btn = QPushButton("Отправить")
        self.send_btn.setFixedHeight(34)
        self.send_btn.setFont(QFont("Segoe UI", 9, QFont.DemiBold))
        self.send_btn.setCursor(Qt.PointingHandCursor)
        self.send_btn.setProperty("accent", True)
        self.send_btn.style().unpolish(self.send_btn)
        self.send_btn.style().polish(self.send_btn)
        self.send_btn.clicked.connect(self.send_message)

        input_row.addWidget(self.input_field, 1)
        input_row.addWidget(self.send_btn)

        # ── Прогресс-лейбл
        self.progress_label = QLabel("")
        self.progress_label.setFont(QFont("Segoe UI", 8))
        self.progress_label.setStyleSheet(f"color: {COLORS['muted']};")
        self.progress_label.setAlignment(Qt.AlignCenter)
        self.progress_label.setVisible(False)

        layout.addLayout(header)
        layout.addWidget(self.chat_display, 1)
        layout.addWidget(chips_label)
        layout.addLayout(chips_row)
        layout.addWidget(all_btn)
        layout.addLayout(input_row)
        layout.addWidget(self.progress_label)
        self.setLayout(layout)

        self.add_message(
            "assistant",
            "Здравствуйте. Выберите период для прогноза или задайте свой вопрос о рынке."
        )

    # ── API (без изменений)
    def update_stock_context(self, stock_data):
        self.current_stock_data = stock_data
        if stock_data is not None and not stock_data.empty:
            last = stock_data.iloc[-1]
            parts = [
                f"Текущая акция: {getattr(self, 'current_stock', 'N/A')}",
                f"Цена: {last['CLOSE']:.2f} ₽",
            ]
            if len(stock_data) > 1:
                prev = stock_data.iloc[-2]
                change = ((last['CLOSE'] - prev['CLOSE']) / prev['CLOSE'] * 100)
                parts.append(f"Изменение: {change:+.2f}%")
            if 'RSI' in stock_data.columns and not pd.isna(last.get('RSI', None)):
                parts.append(f"RSI: {last['RSI']:.1f}")
            if 'VOL' in stock_data.columns:
                volume = last['VOL']
                volume_str = f"{volume / 1_000_000:.1f}M" if volume > 1_000_000 else f"{volume / 1000:.0f}K"
                parts.append(f"Объём: {volume_str}")
            self.stock_context = " | ".join(parts)
        else:
            self.stock_context = "Данные акции недоступны"

    def update_market_data(self, all_stocks_data, market_sentiment):
        self.all_stocks_data = all_stocks_data
        self.market_sentiment = market_sentiment

    def quick_period_query(self, period):
        period_text = self.PERIOD_TEXTS.get(period, period.lower())
        query = (
            f"Какой будет цена акции {self.current_stock} к {period_text}? "
            f"Дай краткий прогноз с учётом текущих данных и рыночной ситуации."
        )
        self.input_field.setText(query)
        self.send_message()

    def quick_all_stocks_query(self):
        query = (
            "Проанализируй все доступные котировки и дай прогноз цен на КОНЕЦ СЛЕДУЮЩЕГО ТОРГОВОГО ДНЯ. "
            "Выдели 3-5 наиболее перспективных акций с конкретными прогнозами цен и обоснованиями. "
            "Укажи ожидаемое изменение в процентах."
        )
        self.input_field.setText(query)
        self.send_message()

    def add_message(self, sender, message):
        timestamp = datetime.now().strftime("%H:%M")
        if sender == "user":
            html = (
                f'<div style="margin:6px 0;">'
                f'<span style="color:{COLORS["muted"]};font-size:11px;">Вы · {timestamp}</span><br/>'
                f'<span style="color:{COLORS["text_strong"]};">{message}</span></div>'
            )
        else:
            html = (
                f'<div style="margin:6px 0;">'
                f'<span style="color:{COLORS["accent_press"]};font-size:11px;font-weight:600;">'
                f'AI · {timestamp}</span><br/>'
                f'<span style="color:{COLORS["text"]};">{message}</span></div>'
            )

        self.chat_display.append(html)
        self.chat_display.verticalScrollBar().setValue(
            self.chat_display.verticalScrollBar().maximum()
        )
        self.chat_history.append({"sender": sender, "message": message, "timestamp": timestamp})
        if len(self.chat_history) > 50:
            self.chat_history = self.chat_history[-50:]

    def update_currency_rates(self, rates_data):
        self.currency_rates = rates_data.get('rates', {})

    def send_message(self):
        message = self.input_field.text().strip()
        if not message or (self.current_worker and self.current_worker.isRunning()):
            return

        prediction_type = "all" if "все" in message.lower() or "котировк" in message.lower() else "single"

        self.add_message("user", message)
        self.input_field.clear()

        self.progress_label.setText(
            "ИИ анализирует данные и риски…"
            if prediction_type == "single"
            else "ИИ анализирует все котировки…"
        )
        self.progress_label.setVisible(True)
        self.send_btn.setEnabled(False)
        self.input_field.setEnabled(False)

        self.current_worker = OllamaWorker(
            message,
            self.stock_context,
            news_data=None,
            stock_data=self.current_stock_data,
            stock_symbol=self.current_stock,
            market_sentiment=self.market_sentiment,
            all_stocks_data=self.all_stocks_data,
            prediction_type=prediction_type,
            currency_rates=self.currency_rates,
        )
        self.current_worker.response_received.connect(self.handle_ai_response)
        self.current_worker.error_occurred.connect(self.handle_ai_error)
        self.current_worker.start()

    def handle_ai_response(self, response):
        self.progress_label.setVisible(False)
        self.send_btn.setEnabled(True)
        self.input_field.setEnabled(True)
        self.add_message("assistant", response)

    def handle_ai_error(self, error_message):
        self.progress_label.setVisible(False)
        self.send_btn.setEnabled(True)
        self.input_field.setEnabled(True)
        self.add_message("assistant", f"⚠ {error_message}")


# =============================================================================
# КУРСЫ ВАЛЮТ
# =============================================================================
class CurrencyRatesWidget(QWidget):
    """Компактная inline-плашка курсов в шапке."""
    rates_updated = pyqtSignal(dict)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.rates = {}
        self.prev_rates = {}
        self.init_ui()
        self.load_currency_rates()

    def init_ui(self):
        layout = QHBoxLayout()
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(14)

        cb = small_label("ЦБ РФ")
        layout.addWidget(cb)

        self.usd_label = QLabel("USD —")
        self.eur_label = QLabel("EUR —")
        self.cny_label = QLabel("CNY —")
        self.inr_label = QLabel("INR —")
        for label in (self.usd_label, self.eur_label, self.cny_label, self.inr_label):
            label.setFont(QFont("Segoe UI", 9))
            label.setStyleSheet(f"color: {COLORS['text_strong']};")
            layout.addWidget(label)

        self.setLayout(layout)

    def load_currency_rates(self):
        try:
            today = datetime.now().strftime("%d/%m/%Y")
            url = f"https://www.cbr.ru/scripts/XML_daily.asp?date_req={today}"
            response = requests.get(url, timeout=10)
            response.raise_for_status()
            root = ET.fromstring(response.content)

            rates = {}
            for valute in root.findall('Valute'):
                char_code = valute.find('CharCode').text
                value = float(valute.find('Value').text.replace(',', '.'))
                nominal = int(valute.find('Nominal').text)
                rates[char_code] = value / nominal

            changes = {}
            for currency, rate in rates.items():
                if currency in self.prev_rates:
                    change = ((rate - self.prev_rates[currency]) / self.prev_rates[currency]) * 100
                    changes[currency] = change

            self.prev_rates = rates.copy()
            self.rates = rates
            self.update_currency_display(changes)
            self.rates_updated.emit({'rates': rates, 'changes': changes})
        except Exception as e:
            print(f"Ошибка загрузки курсов валют: {e}")
            for label, code in (
                (self.usd_label, "USD"), (self.eur_label, "EUR"),
                (self.cny_label, "CNY"), (self.inr_label, "INR"),
            ):
                label.setText(f"{code} —")

    def _format(self, code, rate, change):
        arrow = "▲" if change > 0 else "▼" if change < 0 else "·"
        color = COLORS["success"] if change > 0 else COLORS["danger"] if change < 0 else COLORS["muted"]
        return (
            f"<span style='color:{COLORS['muted']};font-weight:600;'>{code}</span> "
            f"<span style='color:{COLORS['text_strong']};'>{rate:.2f}</span> "
            f"<span style='color:{color};font-size:10px;'>{arrow} {abs(change):.2f}%</span>"
        )

    def update_currency_display(self, changes):
        mapping = {
            'USD': self.usd_label, 'EUR': self.eur_label,
            'CNY': self.cny_label, 'INR': self.inr_label,
        }
        for code, label in mapping.items():
            if code in self.rates:
                label.setText(self._format(code, self.rates[code], changes.get(code, 0)))


# =============================================================================
# НОВОСТИ
# =============================================================================
class NewsWorker(QThread):
    finished = pyqtSignal(list)
    progress = pyqtSignal(str)

    def __init__(self):
        super().__init__()
        self.parser = NewsParser()

    def run(self):
        self.progress.emit("Загрузка новостей…")
        new_news = self.parser.parse_telegram_channel()
        self.finished.emit(new_news)


class NewsItemWidget(QFrame):
    def __init__(self, news_data, parent=None):
        super().__init__(parent)
        self.news_data = news_data
        self.setStyleSheet(f"""
            QFrame {{
                background: transparent;
                border: none;
                border-bottom: 1px solid {COLORS['border']};
            }}
            QFrame:hover {{
                background: {COLORS['panel_alt']};
            }}
        """)

        layout = QVBoxLayout()
        layout.setContentsMargins(2, 8, 2, 8)
        layout.setSpacing(4)

        # Заголовок-строка с датой
        head = QHBoxLayout()
        head.setSpacing(6)
        head.setContentsMargins(0, 0, 0, 0)

        accent = QLabel("●")
        accent.setStyleSheet(f"color: {COLORS['accent']}; font-size: 8px;")
        accent.setFixedWidth(10)

        date_label = QLabel(news_data.get('date', 'Без даты'))
        date_label.setFont(QFont("Segoe UI", 8))
        date_label.setStyleSheet(f"color: {COLORS['muted']};")

        head.addWidget(accent)
        head.addWidget(date_label)
        head.addStretch()

        # Текст
        text = news_data.get('text', '')
        preview = text[:180] + "…" if len(text) > 180 else text
        text_label = QLabel(preview)
        text_label.setFont(QFont("Segoe UI", 9))
        text_label.setStyleSheet(f"color: {COLORS['text_strong']};")
        text_label.setWordWrap(True)

        layout.addLayout(head)
        layout.addWidget(text_label)
        self.setLayout(layout)


class NewsPanel(QFrame):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.news_data = []
        self.current_page = 0
        self.news_per_page = 5
        self.news_worker = None

        self.setObjectName("card")
        self.setStyleSheet(card_style())
        self.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Expanding)

        layout = QVBoxLayout()
        layout.setContentsMargins(16, 14, 16, 14)
        layout.setSpacing(10)

        # Шапка
        header = QHBoxLayout()
        header.addWidget(section_title("Новости"))
        header.addStretch()

        self.refresh_btn = QPushButton("Обновить")
        self.refresh_btn.setFixedHeight(26)
        self.refresh_btn.setFont(QFont("Segoe UI", 9))
        self.refresh_btn.setCursor(Qt.PointingHandCursor)
        self.refresh_btn.clicked.connect(self.refresh_news)
        header.addWidget(self.refresh_btn)

        # Прогресс
        self.progress_label = QLabel("")
        self.progress_label.setFont(QFont("Segoe UI", 8))
        self.progress_label.setStyleSheet(f"color: {COLORS['muted']};")
        self.progress_label.setAlignment(Qt.AlignCenter)
        self.progress_label.setVisible(False)

        # Скролл
        self.scroll_area = QScrollArea()
        self.scroll_area.setWidgetResizable(True)
        self.scroll_area.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        self.scroll_area.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.scroll_area.setStyleSheet("QScrollArea { background: transparent; border: none; }")

        self.news_container = QWidget()
        self.news_container.setStyleSheet("background: transparent;")
        self.news_layout = QVBoxLayout()
        self.news_layout.setContentsMargins(0, 0, 0, 0)
        self.news_layout.setSpacing(0)
        self.news_layout.setAlignment(Qt.AlignTop)
        self.news_container.setLayout(self.news_layout)
        self.scroll_area.setWidget(self.news_container)

        # Кнопка «Показать ещё»
        self.load_more_btn = QPushButton("Показать ещё")
        self.load_more_btn.setFixedHeight(28)
        self.load_more_btn.setFont(QFont("Segoe UI", 9))
        self.load_more_btn.setCursor(Qt.PointingHandCursor)
        self.load_more_btn.clicked.connect(self.load_more_news)

        layout.addLayout(header)
        layout.addWidget(self.progress_label)
        layout.addWidget(self.scroll_area, 1)
        layout.addWidget(self.load_more_btn)
        self.setLayout(layout)

        self.load_news_from_file()

    def load_news_from_file(self):
        parser = NewsParser()
        self.news_data = parser.load_existing_news()
        self.news_data.sort(key=lambda x: x.get('date', ''), reverse=True)
        self.current_page = 0
        self.display_news()

    def refresh_news(self):
        if self.news_worker and self.news_worker.isRunning():
            return
        self.progress_label.setText("Парсинг новостей…")
        self.progress_label.setVisible(True)
        self.refresh_btn.setEnabled(False)

        self.news_worker = NewsWorker()
        self.news_worker.progress.connect(self.progress_label.setText)
        self.news_worker.finished.connect(self.on_news_parsed)
        self.news_worker.start()

    def on_news_parsed(self, new_news):
        self.progress_label.setVisible(False)
        self.refresh_btn.setEnabled(True)

        if new_news:
            self.load_news_from_file()
            self.progress_label.setText(f"Добавлено {len(new_news)} новостей")
        else:
            self.progress_label.setText("Новых новостей не найдено")
        self.progress_label.setVisible(True)
        QTimer.singleShot(3000, lambda: self.progress_label.setVisible(False))

    def display_news(self):
        for i in reversed(range(self.news_layout.count())):
            widget = self.news_layout.itemAt(i).widget()
            if widget:
                widget.setParent(None)

        start_idx = self.current_page * self.news_per_page
        end_idx = start_idx + self.news_per_page
        current = self.news_data[start_idx:end_idx]

        for news in current:
            self.news_layout.addWidget(NewsItemWidget(news))

        has_more = end_idx < len(self.news_data)
        self.load_more_btn.setEnabled(has_more)

        if not has_more and self.news_data:
            self.load_more_btn.setText("Все новости загружены")
        elif not self.news_data:
            self.load_more_btn.setText("Нет новостей")
            placeholder = QLabel("Нажмите «Обновить» для загрузки новостей.")
            placeholder.setFont(QFont("Segoe UI", 9))
            placeholder.setStyleSheet(f"color: {COLORS['muted']}; padding: 24px;")
            placeholder.setAlignment(Qt.AlignCenter)
            placeholder.setWordWrap(True)
            self.news_layout.addWidget(placeholder)

    def load_more_news(self):
        self.current_page += 1
        self.display_news()
        scrollbar = self.scroll_area.verticalScrollBar()
        scrollbar.setValue(0)


# =============================================================================
# ЛИДЕРЫ — ВЕРТИКАЛЬНЫЕ СПИСКИ В ДВА СТОЛБЦА
# =============================================================================
class LeadersWorker(QThread):
    finished = pyqtSignal(dict)
    progress = pyqtSignal(int)

    def __init__(self, stocks):
        super().__init__()
        self.stocks = stocks

    def run(self):
        leaders_data = {}
        total = len(self.stocks)

        for i, (symbol, code) in enumerate(self.stocks.items()):
            try:
                data = self.get_stock_data(symbol, code, 1, 8)
                if data is not None and len(data) >= 2:
                    cur = data['CLOSE'].iloc[-1]
                    prev = data['CLOSE'].iloc[-2]
                    leaders_data[symbol] = {
                        'change': ((cur - prev) / prev) * 100,
                        'price': cur,
                        'volume': data['VOL'].iloc[-1] if 'VOL' in data.columns else 0,
                    }
            except Exception as e:
                print(f"Error loading {symbol}: {e}")
            self.progress.emit(int((i + 1) / total * 100))

        self.finished.emit(leaders_data)

    def get_stock_data(self, stock_code, stock_id, days=1, interval=8):
        end_date = datetime.now()
        start_date = end_date - timedelta(days=days + 5)
        try:
            return fetch_stock_data_with_fallback(
                stock_code=stock_code,
                stock_id=stock_id,
                start_date=start_date,
                end_date=end_date,
                interval=interval,
                timeout=10,
            )
        except Exception:
            return None


class LeaderRow(QFrame):
    """Одна строка списка лидеров: тикер | %изменение | цена."""

    def __init__(self, symbol, change, price, is_gainer=True, parent=None):
        super().__init__(parent)
        self.setFixedHeight(34)
        self.setStyleSheet(f"""
            QFrame {{
                background: transparent;
                border: none;
                border-radius: 6px;
            }}
            QFrame:hover {{
                background: {COLORS['panel_alt']};
            }}
        """)

        color = COLORS['success'] if is_gainer else COLORS['danger']
        sign = '+' if is_gainer else ''

        layout = QHBoxLayout()
        layout.setContentsMargins(10, 0, 10, 0)
        layout.setSpacing(10)

        sym = QLabel(symbol)
        sym.setFont(QFont("Segoe UI", 10, QFont.DemiBold))
        sym.setStyleSheet(f"color: {COLORS['text_strong']};")
        sym.setMinimumWidth(64)

        chg = QLabel(f"{sign}{change:.2f}%")
        chg.setFont(QFont("Segoe UI", 10, QFont.DemiBold))
        chg.setStyleSheet(f"color: {color};")

        prc = QLabel(f"{price:,.2f} ₽".replace(',', ' '))
        prc.setFont(QFont("Segoe UI", 10))
        prc.setStyleSheet(f"color: {COLORS['muted']};")
        prc.setAlignment(Qt.AlignRight | Qt.AlignVCenter)

        layout.addWidget(sym)
        layout.addWidget(chg)
        layout.addStretch()
        layout.addWidget(prc)
        self.setLayout(layout)


class LeadersPanel(QFrame):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("card")
        self.setStyleSheet(card_style())

        outer = QVBoxLayout()
        outer.setContentsMargins(16, 14, 16, 14)
        outer.setSpacing(10)

        # Шапка
        header = QHBoxLayout()
        header.addWidget(section_title("Лидеры дня"))
        header.addStretch()

        self.status_label = QLabel("")
        self.status_label.setFont(QFont("Segoe UI", 8))
        self.status_label.setStyleSheet(f"color: {COLORS['muted']};")
        header.addWidget(self.status_label)

        outer.addLayout(header)

        # Прогресс
        self.progress_bar = QProgressBar()
        self.progress_bar.setFixedHeight(4)
        self.progress_bar.setTextVisible(False)
        self.progress_bar.setVisible(False)
        outer.addWidget(self.progress_bar)

        # Тело: 2 колонки
        body = QHBoxLayout()
        body.setSpacing(20)
        body.setContentsMargins(0, 0, 0, 0)

        # ─── Левый столбец: рост
        gain_col = QVBoxLayout()
        gain_col.setSpacing(2)

        gain_head = QHBoxLayout()
        gain_head.setSpacing(6)
        gain_head.setContentsMargins(2, 0, 2, 4)
        gain_arrow = QLabel("▲")
        gain_arrow.setStyleSheet(f"color: {COLORS['success']}; font-size: 10px;")
        gain_text = QLabel("Лидеры роста")
        gain_text.setFont(QFont("Segoe UI", 9, QFont.DemiBold))
        gain_text.setStyleSheet(f"color: {COLORS['text_strong']};")
        gain_head.addWidget(gain_arrow)
        gain_head.addWidget(gain_text)
        gain_head.addStretch()
        gain_col.addLayout(gain_head)
        gain_col.addWidget(hline())

        self.gainers_container = QVBoxLayout()
        self.gainers_container.setSpacing(0)
        self.gainers_container.setContentsMargins(0, 0, 0, 0)
        gain_col.addLayout(self.gainers_container)
        gain_col.addStretch()

        # ─── Правый столбец: падение
        loss_col = QVBoxLayout()
        loss_col.setSpacing(2)

        loss_head = QHBoxLayout()
        loss_head.setSpacing(6)
        loss_head.setContentsMargins(2, 0, 2, 4)
        loss_arrow = QLabel("▼")
        loss_arrow.setStyleSheet(f"color: {COLORS['danger']}; font-size: 10px;")
        loss_text = QLabel("Лидеры падения")
        loss_text.setFont(QFont("Segoe UI", 9, QFont.DemiBold))
        loss_text.setStyleSheet(f"color: {COLORS['text_strong']};")
        loss_head.addWidget(loss_arrow)
        loss_head.addWidget(loss_text)
        loss_head.addStretch()
        loss_col.addLayout(loss_head)
        loss_col.addWidget(hline())

        self.losers_container = QVBoxLayout()
        self.losers_container.setSpacing(0)
        self.losers_container.setContentsMargins(0, 0, 0, 0)
        loss_col.addLayout(self.losers_container)
        loss_col.addStretch()

        body.addLayout(gain_col, 1)
        body.addWidget(vline())
        body.addLayout(loss_col, 1)

        outer.addLayout(body)
        self.setLayout(outer)

        # начальные пустышки
        self._fill_placeholders()

    def _fill_placeholders(self):
        for cont in (self.gainers_container, self.losers_container):
            for _ in range(5):
                ph = QLabel("—")
                ph.setFixedHeight(34)
                ph.setStyleSheet(f"color: {COLORS['muted_2']}; padding-left: 10px;")
                cont.addWidget(ph)

    def _clear(self, container):
        for i in reversed(range(container.count())):
            widget = container.itemAt(i).widget()
            if widget:
                widget.setParent(None)

    def show_progress(self, show=True):
        self.progress_bar.setVisible(show)
        if show:
            self.progress_bar.setValue(0)
            self.status_label.setText("Загрузка котировок…")

    def update_progress(self, value):
        self.progress_bar.setValue(value)
        if value >= 100:
            self.status_label.setText("Обработка…")

    def update_leaders(self, leaders_data):
        self._clear(self.gainers_container)
        self._clear(self.losers_container)

        if not leaders_data:
            self._fill_placeholders()
            self.show_progress(False)
            self.status_label.setText("Нет данных")
            return

        sorted_data = sorted(leaders_data.items(), key=lambda x: x[1]['change'], reverse=True)
        gainers = [item for item in sorted_data if item[1]['change'] > 0][:5]
        losers = [item for item in sorted_data if item[1]['change'] < 0][-5:]

        for symbol, data in gainers:
            self.gainers_container.addWidget(
                LeaderRow(symbol, data['change'], data['price'], True)
            )
        while self.gainers_container.count() < 5:
            ph = QLabel("—")
            ph.setFixedHeight(34)
            ph.setStyleSheet(f"color: {COLORS['muted_2']}; padding-left: 10px;")
            self.gainers_container.addWidget(ph)

        for symbol, data in losers:
            self.losers_container.addWidget(
                LeaderRow(symbol, data['change'], data['price'], False)
            )
        while self.losers_container.count() < 5:
            ph = QLabel("—")
            ph.setFixedHeight(34)
            ph.setStyleSheet(f"color: {COLORS['muted_2']}; padding-left: 10px;")
            self.losers_container.addWidget(ph)

        self.show_progress(False)
        self.status_label.setText(
            f"Обновлено · {datetime.now().strftime('%H:%M:%S')}"
        )


# =============================================================================
# КАРТОЧКА МЕТРИКИ
# =============================================================================
class MetricWidget(QFrame):
    """Маленькая карточка метрики для сетки."""

    LABELS = {
        "PRICE":  "Цена, ₽",
        "CHANGE": "Изменение",
        "VOLUME": "Объём",
        "RSI":    "RSI 14",
        "MACD":   "MACD",
        "RANGE":  "Диапазон",
    }

    def __init__(self, code, value, parent=None):
        super().__init__(parent)
        self.setMinimumHeight(64)
        self.setStyleSheet(f"""
            QFrame {{
                background: {COLORS['panel_alt']};
                border: 1px solid {COLORS['border']};
                border-radius: 10px;
            }}
        """)

        layout = QVBoxLayout()
        layout.setContentsMargins(12, 8, 12, 8)
        layout.setSpacing(2)

        title_text = self.LABELS.get(code, code)
        self.title_label = QLabel(title_text)
        self.title_label.setFont(QFont("Segoe UI", 8, QFont.DemiBold))
        self.title_label.setStyleSheet(
            f"color: {COLORS['muted']}; letter-spacing: 0.5px;"
        )

        self.value_label = QLabel(value)
        self.value_label.setFont(QFont("Segoe UI", 15, QFont.Bold))
        self.value_label.setStyleSheet(f"color: {COLORS['text_strong']};")

        layout.addWidget(self.title_label)
        layout.addWidget(self.value_label)
        self.setLayout(layout)


# =============================================================================
# ВСПОМОГАТЕЛЬНОЕ
# =============================================================================
class HoverComboBox(QComboBox):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setCursor(Qt.PointingHandCursor)
        self.setMinimumHeight(34)


class AnimatedLabel(QLabel):
    def __init__(self, text="", parent=None):
        super().__init__(text, parent)
        self._opacity = 1.0
        self.animation = QPropertyAnimation(self, b"opacity")
        self.animation.setDuration(500)
        self.animation.setEasingCurve(QEasingCurve.OutCubic)

    @pyqtProperty(float)
    def opacity(self):
        return self._opacity

    @opacity.setter
    def opacity(self, value):
        self._opacity = value
        self.setStyleSheet(f"color: rgba(11, 18, 32, {int(value * 255)});")

    def fade_in(self):
        self.animation.setStartValue(0.0)
        self.animation.setEndValue(1.0)
        self.animation.start()


class HoverButton(QPushButton):
    """Жёлтая акцентная кнопка."""
    def __init__(self, text, parent=None):
        super().__init__(text, parent)
        self.setCursor(Qt.PointingHandCursor)
        self.setFixedHeight(34)
        self.setProperty("accent", True)


# =============================================================================
# ГРАФИК — биржевой dark area внутри обычной светлой темы
# =============================================================================
class StockChartWidget(FigureCanvas):
    def __init__(self, parent=None):
        plt.style.use('dark_background')
        rcParams['axes.facecolor']   = COLORS['chart_bg']
        rcParams['figure.facecolor'] = COLORS['chart_bg']
        rcParams['grid.color']       = COLORS['chart_grid']
        rcParams['lines.linewidth']  = 1.4
        rcParams['text.color']       = COLORS['chart_text']
        rcParams['axes.edgecolor']   = COLORS['chart_grid']
        rcParams['axes.labelcolor']  = COLORS['chart_text']
        rcParams['xtick.color']      = COLORS['chart_text']
        rcParams['ytick.color']      = COLORS['chart_text']

        self.fig = Figure(figsize=(10, 7), dpi=100)
        super().__init__(self.fig)

        self.setParent(parent)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self.setStyleSheet(f"background: {COLORS['chart_bg']};")

        self.current_start_idx = 0
        self.visible_bars = 100
        self.all_data = None
        self.last_mouse_x = 0
        self.dragging = False
        self.symbol = ""

        self.mpl_connect('scroll_event', self.on_scroll)
        self.mpl_connect('button_press_event', self.on_press)
        self.mpl_connect('button_release_event', self.on_release)
        self.mpl_connect('motion_notify_event', self.on_motion)

    def on_scroll(self, event):
        if event.button == 'up':
            self.zoom_in()
        elif event.button == 'down':
            self.zoom_out()
        self.draw()

    def on_press(self, event):
        if event.button == 1 and event.xdata is not None:
            self.dragging = True
            self.last_mouse_x = event.xdata

    def on_release(self, event):
        self.dragging = False

    def on_motion(self, event):
        if self.dragging and event.xdata is not None and self.last_mouse_x is not None:
            try:
                delta = int((self.last_mouse_x - event.xdata) * 5)
                self.scroll(delta)
                self.last_mouse_x = event.xdata
                self.draw()
            except (TypeError, ValueError):
                pass

    def zoom_in(self):
        if self.visible_bars > 20:
            self.visible_bars = max(20, int(self.visible_bars * 0.8))
            self.update_chart()

    def zoom_out(self):
        if self.all_data is not None and self.visible_bars < len(self.all_data):
            self.visible_bars = min(len(self.all_data), int(self.visible_bars * 1.2))
            self.update_chart()

    def scroll(self, delta):
        if self.all_data is None:
            return
        self.current_start_idx = max(
            0, min(len(self.all_data) - self.visible_bars, self.current_start_idx + delta)
        )
        self.update_chart()

    def reset_view(self):
        if self.all_data is not None:
            self.current_start_idx = max(0, len(self.all_data) - self.visible_bars)
            self.update_chart()

    def plot_candlestick(self, data, symbol):
        self.all_data = data
        self.symbol = symbol
        self.current_start_idx = max(0, len(data) - self.visible_bars)
        self.update_chart()

    def update_chart(self):
        if self.all_data is None or len(self.all_data) == 0:
            return

        data = self.all_data
        start_idx = self.current_start_idx
        end_idx = min(start_idx + self.visible_bars, len(data))
        visible = data.iloc[start_idx:end_idx]
        if len(visible) == 0:
            return

        # ── Шаг между барами (в днях, для matplotlib date units)
        # и адаптивный формат даты — иначе 15-минутные свечи слипаются.
        if len(visible) > 1:
            deltas = visible['DATE'].diff().dropna()
            try:
                step_days = deltas.median().total_seconds() / 86400.0
            except AttributeError:
                step_days = 1.0
            if step_days <= 0:
                step_days = 1.0
        else:
            step_days = 1.0

        candle_width = step_days * 0.7  # 70% шага — оставляем зазор между свечами

        # Формат подписей дат в зависимости от диапазона
        try:
            span_days = (visible['DATE'].iloc[-1] - visible['DATE'].iloc[0]).total_seconds() / 86400.0
        except AttributeError:
            span_days = 1.0

        if span_days < 2:
            date_fmt = '%H:%M'
        elif span_days < 60:
            date_fmt = '%d.%m'
        else:
            date_fmt = '%d.%m.%y'

        self.fig.clear()
        gs = self.fig.add_gridspec(
            4, 1, height_ratios=[4, 1, 1.2, 1.2], hspace=0.15
        )

        # ── Цена + свечи
        ax1 = self.fig.add_subplot(gs[0])
        self._plot_candles(ax1, visible, candle_width)
        if 'SMA_20' in visible.columns:
            ax1.plot(visible['DATE'], visible['SMA_20'],
                     color='#FFCC00', linewidth=1.4, alpha=0.9, label='SMA 20')
        if 'SMA_50' in visible.columns:
            ax1.plot(visible['DATE'], visible['SMA_50'],
                     color='#60A5FA', linewidth=1.4, alpha=0.9, label='SMA 50')

        ax1.set_title(f'{self.symbol}',
                      fontsize=12, color='#FFFFFF', pad=10, fontweight='600', loc='left')
        ax1.legend(loc='upper left', framealpha=0.0, fontsize=8, labelcolor='#D1D5DB')
        ax1.grid(True, alpha=0.18, linestyle='-', linewidth=0.5)

        # ── Объёмы
        ax2 = self.fig.add_subplot(gs[1], sharex=ax1)
        colors = [
            '#16A34A' if visible['CLOSE'].iloc[i] >= visible['OPEN'].iloc[i] else '#DC2626'
            for i in range(len(visible))
        ]
        ax2.bar(visible['DATE'], visible['VOL'], color=colors, alpha=0.75, width=candle_width)
        ax2.set_ylabel('Объём', color='#9CA3AF', fontsize=8)
        ax2.grid(True, alpha=0.18, linestyle='-', linewidth=0.5)

        # ── RSI
        ax3 = self.fig.add_subplot(gs[2], sharex=ax1)
        if 'RSI' in visible.columns:
            rsi_data = visible[['DATE', 'RSI']].dropna()
            if len(rsi_data) > 0:
                ax3.plot(rsi_data['DATE'], rsi_data['RSI'],
                         color='#FFCC00', linewidth=1.4, label='RSI')
                ax3.axhline(70, color='#DC2626', linestyle='--', alpha=0.5, linewidth=0.8)
                ax3.axhline(30, color='#16A34A', linestyle='--', alpha=0.5, linewidth=0.8)
                try:
                    ax3.fill_between(rsi_data['DATE'], rsi_data['RSI'], 70,
                                     where=(rsi_data['RSI'] >= 70), color='#DC2626', alpha=0.2)
                    ax3.fill_between(rsi_data['DATE'], rsi_data['RSI'], 30,
                                     where=(rsi_data['RSI'] <= 30), color='#16A34A', alpha=0.2)
                except Exception:
                    pass
                ax3.set_ylabel('RSI', color='#9CA3AF', fontsize=8)
                ax3.set_ylim(0, 100)
        ax3.grid(True, alpha=0.18, linestyle='-', linewidth=0.5)

        # ── MACD
        ax4 = self.fig.add_subplot(gs[3], sharex=ax1)
        macd_cols = ['MACD_12_26_9', 'MACDs_12_26_9', 'MACDh_12_26_9']
        if all(col in visible.columns for col in macd_cols):
            macd_data = visible[['DATE'] + macd_cols].dropna()
            if len(macd_data) > 0:
                ax4.plot(macd_data['DATE'], macd_data['MACD_12_26_9'],
                         color='#FFCC00', linewidth=1.4, label='MACD')
                ax4.plot(macd_data['DATE'], macd_data['MACDs_12_26_9'],
                         color='#60A5FA', linewidth=1.2, label='Signal')
                try:
                    ax4.fill_between(macd_data['DATE'], macd_data['MACDh_12_26_9'], 0, alpha=0.4,
                                     color=['#DC2626' if x < 0 else '#16A34A'
                                            for x in macd_data['MACDh_12_26_9']])
                except Exception:
                    pass
                ax4.set_ylabel('MACD', color='#9CA3AF', fontsize=8)
        ax4.grid(True, alpha=0.18, linestyle='-', linewidth=0.5)

        for ax in (ax1, ax2, ax3, ax4):
            ax.tick_params(colors='#9CA3AF', labelsize=8)
            for spine in ax.spines.values():
                spine.set_color(COLORS['chart_grid'])
            if len(visible) > 0:
                ax.xaxis.set_major_formatter(mdates.DateFormatter(date_fmt))
                ax.xaxis.set_major_locator(mdates.AutoDateLocator(maxticks=8))

        for ax in (ax1, ax2, ax3):
            ax.tick_params(axis='x', labelbottom=False)

        self.fig.subplots_adjust(left=0.06, right=0.98, top=0.96, bottom=0.06)
        self.draw()

    def _plot_candles(self, ax, data, width=0.6):
        """Рисует японские свечи. width — в единицах matplotlib (доля дня)."""
        if len(data) == 0:
            return

        # Толщина «тени» свечи — масштабируется от ширины тела
        wick_lw = max(0.6, min(1.2, width * 60))

        for _, row in data.iterrows():
            date = row['DATE']
            o, h, l, c = row['OPEN'], row['HIGH'], row['LOW'], row['CLOSE']
            if c >= o:
                color = '#16A34A'
                bottom, top = o, c
            else:
                color = '#DC2626'
                bottom, top = c, o

            x = mdates.date2num(date)
            body_h = top - bottom
            if body_h > 0:
                rect = Rectangle(
                    (x - width / 2, bottom),
                    width, body_h,
                    facecolor=color, edgecolor=color, alpha=0.9,
                )
                ax.add_patch(rect)
            else:
                # Доджи — рисуем тонкую горизонтальную линию вместо тела
                ax.plot([x - width / 2, x + width / 2], [c, c],
                        color=color, linewidth=1.0, alpha=0.9)

            # Тени (верхний и нижний хвосты)
            ax.plot([date, date], [l, bottom], color=color, linewidth=wick_lw, alpha=0.9)
            ax.plot([date, date], [top, h],   color=color, linewidth=wick_lw, alpha=0.9)

        if len(data) > 0:
            # Поля по краям пропорциональны ширине свечи (а не фиксированные 12 часов)
            x0 = mdates.date2num(data['DATE'].iloc[0]) - width
            x1 = mdates.date2num(data['DATE'].iloc[-1]) + width
            ax.set_xlim(x0, x1)
            ax.set_ylim(data['LOW'].min() * 0.995, data['HIGH'].max() * 1.005)
