"""Главное окно — Finam-style редизайн (v2): чистая сетка, без фейкового функционала."""

from datetime import datetime, timedelta
import pandas as pd
import pandas_ta as ta

from PyQt5.QtWidgets import (
    QApplication, QMainWindow, QVBoxLayout, QHBoxLayout,
    QWidget, QLabel, QFrame, QPushButton,
    QGridLayout, QSizePolicy, QTabWidget
)
from PyQt5.QtCore import QTimer, Qt
from PyQt5.QtGui import QFont

from ollama_worker import PricePredictor
from app.config import STOCKS, TIME_PERIODS, INTERVALS
from app.data_services import fetch_stock_data_with_fallback
from app.theme import COLORS, build_palette
from app.ui_components import (
    AIChatWidget,
    CurrencyRatesWidget,
    HoverComboBox,
    LeadersPanel,
    LeadersWorker,
    MetricWidget,
    NewsPanel,
    StockChartWidget,
    section_title,
    small_label,
    card_style,
    make_card,
)
from app.agent.agent_panel import AgentPanel


class StockAnalysisApp(QMainWindow):
    def __init__(self):
        super().__init__()
        self.stocks = dict(STOCKS)
        self.time_periods = dict(TIME_PERIODS)
        self.intervals = dict(INTERVALS)

        self.current_data = None
        self.current_stock = "GAZP"
        self.current_period = "1M"
        self.current_interval = "D"
        self.predictor = PricePredictor()
        self.leaders_worker = None

        # ссылки на динамические лейблы hero-блока
        self.ticker_symbol_label = None
        self.ticker_price_label = None
        self.ticker_change_label = None
        self.ticker_meta_label = None

        self.init_ui()

    # =========================================================================
    # UI
    # =========================================================================
    def init_ui(self):
        self.setWindowTitle('Аналитика рынка · MOEX')
        self.resize(1640, 960)
        self.setMinimumSize(1320, 800)

        palette = build_palette()
        self.setPalette(palette)

        central = QWidget()
        self.setCentralWidget(central)

        outer = QVBoxLayout()
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        # Шапка
        outer.addWidget(self._build_header())

        # Hero-блок тикера
        outer.addWidget(self._build_hero())

        # Вкладки: аналитика и AI-агент
        self.tabs = QTabWidget()
        self.tabs.setDocumentMode(True)
        self.tabs.setStyleSheet(
            f"QTabWidget::pane {{ background: {COLORS['bg']}; border: none; }}"
            f"QTabBar::tab {{ background: {COLORS['panel_alt']}; color: {COLORS['muted']}; "
            f"padding: 8px 18px; border-top-left-radius: 8px; border-top-right-radius: 8px; "
            f"margin-right: 4px; font-weight: 600; }}"
            f"QTabBar::tab:selected {{ background: {COLORS['panel']}; color: {COLORS['text_strong']}; "
            f"border-bottom: 2px solid {COLORS['accent']}; }}"
        )

        # Тело: основная зона + сайдбар
        body = QWidget()
        body.setStyleSheet(f"background: {COLORS['bg']};")
        body_layout = QHBoxLayout()
        body_layout.setContentsMargins(20, 16, 20, 16)
        body_layout.setSpacing(16)

        body_layout.addWidget(self._build_main_column(), 1)
        body_layout.addWidget(self._build_sidebar())

        body.setLayout(body_layout)
        self.tabs.addTab(body, "Аналитика")

        # Вкладка AI-агента
        self.agent_panel = AgentPanel(stocks_map=self.stocks, predictor=self.predictor)
        self.tabs.addTab(self.agent_panel, "AI-агент")

        outer.addWidget(self.tabs, 1)

        central.setLayout(outer)
        central.setStyleSheet(f"background: {COLORS['bg']};")

        # Первичная загрузка
        QTimer.singleShot(100, lambda: self.on_stock_selected("GAZP"))
        QTimer.singleShot(500, self.load_leaders_data)

    # ─────────────────────────────────────────────────────────────────────────
    # Шапка
    # ─────────────────────────────────────────────────────────────────────────
    def _build_header(self) -> QWidget:
        bar = QFrame()
        bar.setFixedHeight(60)
        bar.setStyleSheet(f"""
            QFrame {{
                background: {COLORS['panel']};
                border: none;
                border-bottom: 1px solid {COLORS['border']};
            }}
        """)

        layout = QHBoxLayout()
        layout.setContentsMargins(20, 0, 20, 0)
        layout.setSpacing(20)

        # ── Бренд: жёлтая полоска + название
        brand = QHBoxLayout()
        brand.setSpacing(10)

        brand_mark = QLabel()
        brand_mark.setFixedSize(4, 28)
        brand_mark.setStyleSheet(
            f"background: {COLORS['accent']}; border-radius: 2px;"
        )

        brand_title = QLabel("Аналитика рынка")
        brand_title.setFont(QFont("Segoe UI", 13, QFont.Bold))
        brand_title.setStyleSheet(f"color: {COLORS['text_strong']};")

        brand_sub = QLabel("MOEX")
        brand_sub.setFont(QFont("Segoe UI", 9, QFont.DemiBold))
        brand_sub.setStyleSheet(
            f"color: {COLORS['muted']}; "
            f"background: {COLORS['panel_alt']}; "
            f"border: 1px solid {COLORS['border']}; "
            f"border-radius: 4px; padding: 2px 6px;"
        )

        brand.addWidget(brand_mark)
        brand.addWidget(brand_title)
        brand.addWidget(brand_sub)

        # ── Контролы: акция / период / интервал
        controls = QHBoxLayout()
        controls.setSpacing(10)
        controls.addLayout(self._field("Инструмент", self._make_stock_combo(), 140))
        controls.addLayout(self._field("Период",     self._make_period_combo(), 96))
        controls.addLayout(self._field("Интервал",   self._make_interval_combo(), 86))

        # ── Курсы валют
        self.currency_rates_widget = CurrencyRatesWidget()
        self.currency_rates_widget.rates_updated.connect(self.on_currency_rates_updated)

        # ── Кнопка обновления
        self.refresh_btn = QPushButton("Обновить")
        self.refresh_btn.setFixedHeight(34)
        self.refresh_btn.setMinimumWidth(108)
        self.refresh_btn.setCursor(Qt.PointingHandCursor)
        self.refresh_btn.setFont(QFont("Segoe UI", 9, QFont.DemiBold))
        self.refresh_btn.setProperty("accent", True)
        self.refresh_btn.style().unpolish(self.refresh_btn)
        self.refresh_btn.style().polish(self.refresh_btn)
        self.refresh_btn.clicked.connect(self.on_update_clicked)

        layout.addLayout(brand)
        layout.addSpacing(20)
        layout.addLayout(controls)
        layout.addStretch()
        layout.addWidget(self.currency_rates_widget)
        layout.addSpacing(10)
        layout.addWidget(self.refresh_btn)

        bar.setLayout(layout)
        return bar

    def _field(self, label_text: str, control: QWidget, width: int) -> QVBoxLayout:
        col = QVBoxLayout()
        col.setSpacing(2)
        col.setContentsMargins(0, 0, 0, 0)
        lbl = small_label(label_text)
        col.addWidget(lbl)
        control.setFixedWidth(width)
        col.addWidget(control)
        return col

    def _make_stock_combo(self) -> HoverComboBox:
        self.stock_combo = HoverComboBox()
        self.stock_combo.setFont(QFont("Segoe UI", 10, QFont.DemiBold))
        self.stock_combo.addItems(self.stocks.keys())
        self.stock_combo.currentTextChanged.connect(self.on_stock_selected)
        return self.stock_combo

    def _make_period_combo(self) -> HoverComboBox:
        self.period_combo = HoverComboBox()
        self.period_combo.setFont(QFont("Segoe UI", 10))
        self.period_combo.addItems(self.time_periods.keys())
        self.period_combo.setCurrentText("1M")
        self.period_combo.currentTextChanged.connect(self.on_period_changed)
        return self.period_combo

    def _make_interval_combo(self) -> HoverComboBox:
        self.interval_combo = HoverComboBox()
        self.interval_combo.setFont(QFont("Segoe UI", 10))
        items = []
        items.extend([f"M{m}" for m in [1, 2, 3, 4, 5, 6, 10, 12, 15, 20, 30]])
        items.extend([f"H{h}" for h in [1, 2, 3, 4, 6, 8, 12]])
        items.extend(["D", "W", "MN", "QR"])
        self.interval_combo.addItems(items)
        self.interval_combo.setCurrentText("D")
        self.interval_combo.currentTextChanged.connect(self.on_interval_changed)
        return self.interval_combo

    # ─────────────────────────────────────────────────────────────────────────
    # Hero-блок
    # ─────────────────────────────────────────────────────────────────────────
    def _build_hero(self) -> QWidget:
        hero = QFrame()
        hero.setStyleSheet(f"""
            QFrame {{
                background: {COLORS['panel']};
                border: none;
                border-bottom: 1px solid {COLORS['border']};
            }}
        """)

        layout = QHBoxLayout()
        layout.setContentsMargins(20, 16, 20, 16)
        layout.setSpacing(20)

        # Левая часть: тикер + цена + изменение
        left = QVBoxLayout()
        left.setSpacing(4)

        # Заголовок-тикер
        tname = QHBoxLayout()
        tname.setSpacing(10)
        tname.setContentsMargins(0, 0, 0, 0)

        self.ticker_symbol_label = QLabel("GAZP")
        self.ticker_symbol_label.setFont(QFont("Segoe UI", 22, QFont.Bold))
        self.ticker_symbol_label.setStyleSheet(f"color: {COLORS['text_strong']};")

        tname.addWidget(self.ticker_symbol_label)
        tname.addStretch()

        # Цена + изменение в одном ряду
        price_row = QHBoxLayout()
        price_row.setSpacing(16)
        price_row.setContentsMargins(0, 0, 0, 0)
        price_row.setAlignment(Qt.AlignLeft | Qt.AlignVCenter)

        self.ticker_price_label = QLabel("—")
        self.ticker_price_label.setFont(QFont("Segoe UI", 28, QFont.Bold))
        self.ticker_price_label.setStyleSheet(f"color: {COLORS['text_strong']};")

        self.ticker_change_label = QLabel("")
        self.ticker_change_label.setFont(QFont("Segoe UI", 13, QFont.DemiBold))

        price_row.addWidget(self.ticker_price_label)
        price_row.addWidget(self.ticker_change_label)
        price_row.addStretch()

        # Мета-строка
        self.ticker_meta_label = QLabel("Биржа MOEX · валюта RUB")
        self.ticker_meta_label.setFont(QFont("Segoe UI", 9))
        self.ticker_meta_label.setStyleSheet(f"color: {COLORS['muted']};")

        left.addLayout(tname)
        left.addLayout(price_row)
        left.addWidget(self.ticker_meta_label)

        layout.addLayout(left, 1)
        hero.setLayout(layout)
        return hero

    # ─────────────────────────────────────────────────────────────────────────
    # Основная колонка: график + лидеры
    # ─────────────────────────────────────────────────────────────────────────
    def _build_main_column(self) -> QWidget:
        wrap = QWidget()
        wrap.setStyleSheet("background: transparent;")
        layout = QVBoxLayout()
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(14)

        # ── График в карточке (тёмная подложка с padding-ом)
        chart_card = QFrame()
        chart_card.setObjectName("chart-card")
        chart_card.setStyleSheet(f"""
            QFrame#chart-card {{
                background: {COLORS['chart_bg']};
                border: 1px solid {COLORS['border']};
                border-radius: 12px;
            }}
        """)
        chart_card.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)

        chart_layout = QVBoxLayout()
        chart_layout.setContentsMargins(8, 8, 8, 8)
        chart_layout.setSpacing(0)

        self.chart_widget = StockChartWidget()
        chart_layout.addWidget(self.chart_widget)
        chart_card.setLayout(chart_layout)

        # ── Лидеры
        self.leaders_panel = LeadersPanel()

        layout.addWidget(chart_card, 1)
        layout.addWidget(self.leaders_panel)

        wrap.setLayout(layout)
        return wrap

    # ─────────────────────────────────────────────────────────────────────────
    # Сайдбар: метрики, прогноз, ИИ, новости
    # ─────────────────────────────────────────────────────────────────────────
    def _build_sidebar(self) -> QWidget:
        wrap = QWidget()
        wrap.setFixedWidth(420)
        wrap.setStyleSheet("background: transparent;")

        layout = QVBoxLayout()
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(14)

        layout.addWidget(self._build_metrics_card())
        layout.addWidget(self._build_prediction_card())

        self.ai_chat_widget = AIChatWidget()
        layout.addWidget(self.ai_chat_widget, 2)

        self.news_panel = NewsPanel()
        layout.addWidget(self.news_panel, 1)

        wrap.setLayout(layout)
        return wrap

    # ── Карточка метрик
    def _build_metrics_card(self) -> QFrame:
        card = make_card()

        layout = QVBoxLayout()
        layout.setContentsMargins(16, 14, 16, 14)
        layout.setSpacing(10)

        layout.addWidget(section_title("Текущие метрики"))

        grid = QGridLayout()
        grid.setSpacing(8)
        grid.setContentsMargins(0, 0, 0, 0)

        self.metric_widgets = {}
        items = [
            ("PRICE",  0, 0),
            ("CHANGE", 0, 1),
            ("VOLUME", 0, 2),
            ("RSI",    1, 0),
            ("MACD",   1, 1),
            ("RANGE",  1, 2),
        ]
        for code, row, col in items:
            w = MetricWidget(code, "—")
            grid.addWidget(w, row, col)
            self.metric_widgets[code] = w

        layout.addLayout(grid)
        card.setLayout(layout)
        return card

    # ── Карточка прогноза
    def _build_prediction_card(self) -> QFrame:
        card = make_card()

        layout = QVBoxLayout()
        layout.setContentsMargins(16, 14, 16, 14)
        layout.setSpacing(6)

        # Шапка
        head = QHBoxLayout()
        head.setSpacing(8)
        head.setContentsMargins(0, 0, 0, 0)
        head.addWidget(section_title("Прогноз на завтра"))
        head.addStretch()

        layout.addLayout(head)

        # Большое число
        self.prediction_label = QLabel("н/д")
        self.prediction_label.setFont(QFont("Segoe UI", 24, QFont.Bold))
        self.prediction_label.setStyleSheet(f"color: {COLORS['muted']};")
        self.prediction_label.setAlignment(Qt.AlignLeft)

        # Строка изменения
        self.prediction_change_label = QLabel("")
        self.prediction_change_label.setFont(QFont("Segoe UI", 11, QFont.DemiBold))
        self.prediction_change_label.setAlignment(Qt.AlignLeft)

        # Точность модели
        self.prediction_accuracy_label = QLabel("")
        self.prediction_accuracy_label.setFont(QFont("Segoe UI", 9))
        self.prediction_accuracy_label.setStyleSheet(f"color: {COLORS['muted']};")
        self.prediction_accuracy_label.setAlignment(Qt.AlignLeft)
        self.prediction_accuracy_label.setWordWrap(True)

        layout.addWidget(self.prediction_label)
        layout.addWidget(self.prediction_change_label)
        layout.addWidget(self.prediction_accuracy_label)
        card.setLayout(layout)
        return card

    # =========================================================================
    # Логика — без изменений в публичном API
    # =========================================================================
    def on_currency_rates_updated(self, rates_data):
        if hasattr(self, 'ai_chat_widget'):
            self.ai_chat_widget.update_currency_rates(rates_data)

    def load_leaders_data(self):
        if self.leaders_worker and self.leaders_worker.isRunning():
            return
        self.leaders_panel.show_progress(True)
        self.leaders_worker = LeadersWorker(self.stocks)
        self.leaders_worker.progress.connect(self.leaders_panel.update_progress)
        self.leaders_worker.finished.connect(self.on_leaders_loaded)
        self.leaders_worker.start()

    def on_leaders_loaded(self, leaders_data):
        self.leaders_panel.update_leaders(leaders_data)
        self.update_market_sentiment(leaders_data)

    def get_stock_data(self, stock_code, days=30, interval=8):
        end_date = datetime.now()
        start_date = end_date - timedelta(days=days)
        try:
            df = fetch_stock_data_with_fallback(
                stock_code=stock_code,
                stock_id=self.stocks[stock_code],
                start_date=start_date,
                end_date=end_date,
                interval=interval,
                timeout=15,
            )
            if not df.empty:
                df = self.calculate_indicators(df)
            return df
        except Exception as e:
            print(f"Error fetching data for {stock_code}: {e}")
            return None

    def update_market_sentiment(self, leaders_data):
        if not leaders_data:
            return
        try:
            positive = sum(1 for d in leaders_data.values() if d['change'] > 0)
            total = len(leaders_data)
            avg_change = sum(d['change'] for d in leaders_data.values()) / total
            ratio = (positive / total) * 100
            if ratio > 60 and avg_change > 0.5:
                sentiment = f"📈 Бычий рынок ({ratio:.1f}% акций в плюсе)"
            elif ratio < 40 and avg_change < -0.5:
                sentiment = f"📉 Медвежий рынок ({ratio:.1f}% акций в плюсе)"
            else:
                sentiment = f"➡️ Нейтральный рынок ({ratio:.1f}% акций в плюсе)"
            self.ai_chat_widget.update_market_data(leaders_data, sentiment)
        except Exception as e:
            print(f"Ошибка обновления настроения рынка: {e}")

    def calculate_indicators(self, df):
        try:
            if len(df) >= 14:
                df['RSI'] = ta.rsi(df['CLOSE'], length=14)
            if len(df) >= 26:
                macd = ta.macd(df['CLOSE'])
                if macd is not None and len(macd) == len(df):
                    df = pd.concat([df, macd], axis=1)
            if len(df) >= 20:
                df['SMA_20'] = ta.sma(df['CLOSE'], length=20)
            if len(df) >= 50:
                df['SMA_50'] = ta.sma(df['CLOSE'], length=50)
        except Exception as e:
            print(f"Ошибка расчёта индикаторов: {e}")
        return df

    # ─── обработчики
    def on_stock_selected(self, stock_code):
        if stock_code in self.stocks:
            self.current_stock = stock_code
            if self.ticker_symbol_label is not None:
                self.ticker_symbol_label.setText(stock_code)
            self.load_stock_data()

    def on_period_changed(self, period):
        if period in self.time_periods:
            self.current_period = period
            self.load_stock_data()

    def on_interval_changed(self, interval):
        if interval in self.intervals:
            self.current_interval = interval
            self.load_stock_data()

    def on_update_clicked(self):
        self.load_stock_data()
        self.load_leaders_data()

    # ─── загрузка
    def load_stock_data(self):
        days = self.time_periods[self.current_period]
        interval_code = self.intervals[self.current_interval]

        self.prediction_label.setText("Расчёт…")
        self.prediction_label.setStyleSheet(f"color: {COLORS['muted']};")
        self.prediction_change_label.setText("")
        self.prediction_change_label.setStyleSheet("")
        self.prediction_accuracy_label.setText("")
        QApplication.processEvents()

        data = self.get_stock_data(self.current_stock, days, interval_code)

        if data is not None and not data.empty:
            self.current_data = data
            self.update_metrics(data)
            self.update_ticker_header(data)
            self.chart_widget.plot_candlestick(data, self.current_stock)
            self.ai_chat_widget.update_stock_context(data)
            self.ai_chat_widget.current_stock = self.current_stock
            self.make_prediction(data)
        else:
            self.prediction_label.setText("н/д")
            self.prediction_label.setStyleSheet(f"color: {COLORS['muted']};")

    def update_ticker_header(self, data):
        if data is None or data.empty:
            return
        last = data.iloc[-1]
        price = last['CLOSE']
        self.ticker_price_label.setText(f"{price:,.2f} ₽".replace(",", " "))

        if len(data) > 1:
            prev = data.iloc[-2]
            change_abs = last['CLOSE'] - prev['CLOSE']
            change_pct = (change_abs / prev['CLOSE']) * 100
        else:
            change_abs = 0
            change_pct = 0

        is_up = change_pct >= 0
        color = COLORS['success'] if is_up else COLORS['danger']
        bg    = COLORS['success_bg'] if is_up else COLORS['danger_bg']
        arrow = "▲" if is_up else "▼"
        sign  = "+" if is_up else ""

        self.ticker_change_label.setText(
            f"{arrow}  {sign}{change_abs:.2f} ₽    {sign}{change_pct:.2f}%"
        )
        self.ticker_change_label.setStyleSheet(
            f"color: {color}; background: {bg}; padding: 6px 12px; border-radius: 8px;"
        )

        self.ticker_meta_label.setText(
            f"Биржа MOEX · валюта RUB · обновлено {datetime.now().strftime('%H:%M:%S')}"
        )

    def make_prediction(self, data):
        if len(data) < 30:
            self.prediction_label.setText("н/д")
            self.prediction_label.setStyleSheet(f"color: {COLORS['muted']};")
            return

        current_price = data['CLOSE'].iloc[-1]
        predicted, accuracy, _ = self.predictor.predict_next_day(data, current_price)

        if predicted is not None:
            change = ((predicted - current_price) / current_price) * 100
            color = COLORS['success'] if change >= 0 else COLORS['danger']
            arrow = "▲" if change >= 0 else "▼"

            self.prediction_label.setText(f"{predicted:,.2f} ₽".replace(",", " "))
            self.prediction_label.setStyleSheet(f"color: {COLORS['text_strong']};")

            self.prediction_change_label.setText(f"{arrow}  {change:+.2f}% от текущей цены")
            self.prediction_change_label.setStyleSheet(f"color: {color};")

            if accuracy is not None:
                acc_color = (
                    COLORS['success'] if accuracy > 70
                    else COLORS['accent_press'] if accuracy > 50
                    else COLORS['danger']
                )
                self.prediction_accuracy_label.setText(f"Угадывает направление · {accuracy:.1f}%")
                self.prediction_accuracy_label.setStyleSheet(f"color: {acc_color};")
        else:
            self.prediction_label.setText("н/д")
            self.prediction_label.setStyleSheet(f"color: {COLORS['muted']};")

    def update_metrics(self, data):
        if len(data) == 0:
            return
        last = data.iloc[-1]

        # Цена
        self.metric_widgets["PRICE"].value_label.setText(f"{last['CLOSE']:.2f}")
        self.metric_widgets["PRICE"].value_label.setStyleSheet(
            f"color: {COLORS['text_strong']};"
        )

        # Изменение
        if len(data) > 1:
            prev = data.iloc[-2]
            pct = ((last['CLOSE'] - prev['CLOSE']) / prev['CLOSE'] * 100)
        else:
            pct = 0
        change_color = COLORS['success'] if pct >= 0 else COLORS['danger']
        sign = "+" if pct >= 0 else ""
        self.metric_widgets["CHANGE"].value_label.setText(f"{sign}{pct:.2f}%")
        self.metric_widgets["CHANGE"].value_label.setStyleSheet(f"color: {change_color};")

        # Объём
        vol = last['VOL']
        vol_text = f"{vol / 1_000_000:.1f}M" if vol > 1_000_000 else f"{vol / 1000:.0f}K"
        self.metric_widgets["VOLUME"].value_label.setText(vol_text)
        self.metric_widgets["VOLUME"].value_label.setStyleSheet(
            f"color: {COLORS['info']};"
        )

        # RSI
        if 'RSI' in data.columns and not pd.isna(last['RSI']):
            rsi_value = last['RSI']
            rsi_color = (
                COLORS['danger'] if rsi_value > 70
                else COLORS['success'] if rsi_value < 30
                else COLORS['accent_press']
            )
            self.metric_widgets["RSI"].value_label.setText(f"{rsi_value:.1f}")
            self.metric_widgets["RSI"].value_label.setStyleSheet(f"color: {rsi_color};")
        else:
            self.metric_widgets["RSI"].value_label.setText("—")
            self.metric_widgets["RSI"].value_label.setStyleSheet(f"color: {COLORS['muted']};")

        # MACD
        if 'MACD_12_26_9' in data.columns and not pd.isna(last['MACD_12_26_9']):
            macd = last['MACD_12_26_9']
            macd_color = COLORS['success'] if macd > 0 else COLORS['danger']
            self.metric_widgets["MACD"].value_label.setText(f"{macd:.3f}")
            self.metric_widgets["MACD"].value_label.setStyleSheet(f"color: {macd_color};")
        else:
            self.metric_widgets["MACD"].value_label.setText("—")
            self.metric_widgets["MACD"].value_label.setStyleSheet(f"color: {COLORS['muted']};")

        # Диапазон
        rng = last['HIGH'] - last['LOW']
        self.metric_widgets["RANGE"].value_label.setText(f"{rng:.2f}")
        self.metric_widgets["RANGE"].value_label.setStyleSheet(
            f"color: {COLORS['accent_press']};"
        )

        # Возвращаем значениям единый размер шрифта
        for code in ("PRICE", "CHANGE", "VOLUME", "RSI", "MACD", "RANGE"):
            self.metric_widgets[code].value_label.setFont(QFont("Segoe UI", 15, QFont.Bold))
