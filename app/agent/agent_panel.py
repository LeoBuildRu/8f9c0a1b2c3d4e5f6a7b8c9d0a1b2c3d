"""Виджет-панель управления торговым агентом."""

from __future__ import annotations

import random
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

from PyQt5.QtCore import Qt, QThread, QTimer, pyqtSignal
from PyQt5.QtGui import QColor, QFont
from PyQt5.QtWidgets import (
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QProgressBar,
    QPushButton,
    QSizePolicy,
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from app.agent.trader_agent import (
    AGENT_CAPITAL_LIMIT,
    AgentTickReport,
    TraderAgent,
)
from app.agent.qt_worker import TraderAgentWorker
from app.theme import COLORS
from app.ui_components import card_style, make_card, section_title, small_label


# Рандомный интервал в миллисекундах между авто-событиями.
AUTORUN_MIN_MIN = 10
AUTORUN_MAX_MIN = 30


def _random_interval_ms() -> int:
    return random.randint(AUTORUN_MIN_MIN * 60_000, AUTORUN_MAX_MIN * 60_000)


class NewsRefreshWorker(QThread):
    """Запускает существующий NewsParser в отдельном потоке."""

    finished_with_count = pyqtSignal(int)
    error_occurred = pyqtSignal(str)

    def run(self) -> None:
        try:
            # Импортируем лениво — Playwright тяжёлый.
            from ollama_worker import NewsParser
            parser = NewsParser()
            new_items = parser.parse_telegram_channel()
            self.finished_with_count.emit(len(new_items) if new_items else 0)
        except Exception as exc:
            self.error_occurred.emit(f"{type(exc).__name__}: {exc}")


def _fmt_money(value: float) -> str:
    return f"{value:,.2f} ₽".replace(",", " ")


def _fmt_pct(value: float) -> str:
    return f"{value:+.2f}%"


class _Stat(QWidget):
    def __init__(self, title: str, value: str = "—"):
        super().__init__()
        self.setStyleSheet("background: transparent;")
        layout = QVBoxLayout()
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(2)
        self.title_lbl = small_label(title)
        self.value_lbl = QLabel(value)
        self.value_lbl.setFont(QFont("Segoe UI", 14, QFont.Bold))
        self.value_lbl.setStyleSheet(f"color: {COLORS['text_strong']};")
        layout.addWidget(self.title_lbl)
        layout.addWidget(self.value_lbl)
        self.setLayout(layout)

    def set_value(self, value: str, color: Optional[str] = None) -> None:
        self.value_lbl.setText(value)
        color = color or COLORS["text_strong"]
        self.value_lbl.setStyleSheet(f"color: {color};")


class AgentPanel(QWidget):
    """Основной виджет: статус, портфель, открытые сделки, журнал, уроки."""

    def __init__(self, stocks_map: Dict[str, int], predictor=None, parent=None):
        super().__init__(parent)
        self.agent = TraderAgent(
            stocks_map=stocks_map,
            predictor=predictor,
            capital_limit=AGENT_CAPITAL_LIMIT,
        )
        self._worker: Optional[TraderAgentWorker] = None
        self._news_worker: Optional[NewsRefreshWorker] = None

        # Два независимых таймера-«одноразовика» — мы перезапускаем их с
        # рандомной задержкой 10–30 минут, чтобы создать естественный график.
        self._tick_timer = QTimer(self)
        self._tick_timer.setSingleShot(True)
        self._tick_timer.timeout.connect(self._scheduled_tick)

        self._news_timer = QTimer(self)
        self._news_timer.setSingleShot(True)
        self._news_timer.timeout.connect(self._scheduled_news_refresh)

        self._autorun = False
        self._next_tick_at: Optional[datetime] = None
        self._next_news_at: Optional[datetime] = None

        # Лёгкое отображение «когда следующий запуск» — раз в секунду.
        self._countdown_timer = QTimer(self)
        self._countdown_timer.setInterval(1_000)
        self._countdown_timer.timeout.connect(self._update_countdown_labels)

        self._build_ui()
        self.refresh_state()

    # ── Сборка UI ────────────────────────────────────────────────────────────
    def _build_ui(self) -> None:
        self.setStyleSheet(f"background: {COLORS['bg']};")
        root = QVBoxLayout()
        root.setContentsMargins(16, 16, 16, 16)
        root.setSpacing(12)

        root.addWidget(self._build_header())
        root.addWidget(self._build_portfolio_card())

        tabs = QTabWidget()
        tabs.setDocumentMode(True)
        tabs.addTab(self._build_open_trades_tab(), "Открытые позиции")
        tabs.addTab(self._build_decisions_tab(), "Журнал решений")
        tabs.addTab(self._build_closed_tab(), "Закрытые сделки")
        tabs.addTab(self._build_lessons_tab(), "Уроки")
        tabs.addTab(self._build_last_tick_tab(), "Последний тик")
        root.addWidget(tabs, 1)

        self.setLayout(root)

    def _build_header(self) -> QWidget:
        card = make_card()
        outer = QVBoxLayout()
        outer.setContentsMargins(16, 14, 16, 14)
        outer.setSpacing(8)

        row1 = QHBoxLayout()
        row1.setSpacing(16)

        title = QLabel("AI-агент трейдинга MOEX")
        title.setFont(QFont("Segoe UI", 14, QFont.Bold))
        title.setStyleSheet(f"color: {COLORS['text_strong']};")

        self.status_lbl = QLabel("Готов")
        self.status_lbl.setFont(QFont("Segoe UI", 10, QFont.DemiBold))
        self.status_lbl.setStyleSheet(
            f"color: {COLORS['muted']}; background: {COLORS['panel_alt']}; "
            f"border: 1px solid {COLORS['border']}; border-radius: 8px; padding: 4px 10px;"
        )

        self.tick_btn = QPushButton("▶ Запустить цикл")
        self.tick_btn.setProperty("accent", True)
        self.tick_btn.setCursor(Qt.PointingHandCursor)
        self.tick_btn.style().unpolish(self.tick_btn)
        self.tick_btn.style().polish(self.tick_btn)
        self.tick_btn.clicked.connect(self.run_tick)

        self.refresh_news_btn = QPushButton("↻ Обновить новости")
        self.refresh_news_btn.setCursor(Qt.PointingHandCursor)
        self.refresh_news_btn.clicked.connect(self.refresh_news)

        self.auto_btn = QPushButton("Авто-режим: выкл")
        self.auto_btn.setCheckable(True)
        self.auto_btn.setCursor(Qt.PointingHandCursor)
        self.auto_btn.toggled.connect(self._toggle_autorun)

        self.reset_btn = QPushButton("Сбросить")
        self.reset_btn.setCursor(Qt.PointingHandCursor)
        self.reset_btn.clicked.connect(self._reset_state)

        self.progress = QProgressBar()
        self.progress.setRange(0, 0)
        self.progress.setVisible(False)
        self.progress.setFixedWidth(110)

        row1.addWidget(title)
        row1.addWidget(self.status_lbl)
        row1.addStretch()
        row1.addWidget(self.progress)
        row1.addWidget(self.refresh_news_btn)
        row1.addWidget(self.auto_btn)
        row1.addWidget(self.reset_btn)
        row1.addWidget(self.tick_btn)

        row2 = QHBoxLayout()
        row2.setSpacing(16)
        self.next_tick_lbl = QLabel("След. проверка позиций: —")
        self.next_tick_lbl.setFont(QFont("Segoe UI", 9))
        self.next_tick_lbl.setStyleSheet(f"color: {COLORS['muted']};")
        self.next_news_lbl = QLabel("След. обновление новостей: —")
        self.next_news_lbl.setFont(QFont("Segoe UI", 9))
        self.next_news_lbl.setStyleSheet(f"color: {COLORS['muted']};")
        row2.addWidget(self.next_tick_lbl)
        row2.addWidget(self.next_news_lbl)
        row2.addStretch()

        outer.addLayout(row1)
        outer.addLayout(row2)
        card.setLayout(outer)
        return card

    def _build_portfolio_card(self) -> QWidget:
        card = make_card()
        layout = QVBoxLayout()
        layout.setContentsMargins(16, 14, 16, 14)
        layout.setSpacing(10)

        head = QHBoxLayout()
        head.addWidget(section_title("Портфель агента"))
        head.addStretch()
        self.limit_lbl = QLabel(f"Лимит капитала: {_fmt_money(AGENT_CAPITAL_LIMIT)}")
        self.limit_lbl.setFont(QFont("Segoe UI", 9))
        self.limit_lbl.setStyleSheet(f"color: {COLORS['muted']};")
        head.addWidget(self.limit_lbl)
        layout.addLayout(head)

        grid = QGridLayout()
        grid.setHorizontalSpacing(20)
        grid.setVerticalSpacing(8)

        self.stat_equity = _Stat("СОБСТВЕННЫЙ КАПИТАЛ")
        self.stat_cash = _Stat("СВОБОДНЫЙ КЭШ")
        self.stat_invested = _Stat("В ПОЗИЦИЯХ")
        self.stat_return = _Stat("ОБЩАЯ ДОХОДНОСТЬ")
        self.stat_open = _Stat("ОТКРЫТО ПОЗИЦИЙ")
        self.stat_winrate = _Stat("WINRATE")
        self.stat_realized = _Stat("РЕАЛИЗ. P&L")
        self.stat_closed = _Stat("СДЕЛОК ЗАКРЫТО")

        widgets = [
            self.stat_equity, self.stat_cash, self.stat_invested, self.stat_return,
            self.stat_open, self.stat_winrate, self.stat_realized, self.stat_closed,
        ]
        for i, w in enumerate(widgets):
            grid.addWidget(w, i // 4, i % 4)
        layout.addLayout(grid)
        card.setLayout(layout)
        return card

    # ── Tabs ─────────────────────────────────────────────────────────────────
    def _build_open_trades_tab(self) -> QWidget:
        wrap = QWidget()
        layout = QVBoxLayout()
        layout.setContentsMargins(0, 8, 0, 0)
        layout.setSpacing(8)
        self.open_table = QTableWidget(0, 8)
        self.open_table.setHorizontalHeaderLabels([
            "Тикер", "Кол-во", "Вход", "SL", "TP", "Открыто", "Дедлайн", "Горизонт ч",
        ])
        self._configure_table(self.open_table)
        layout.addWidget(self.open_table)
        wrap.setLayout(layout)
        return wrap

    def _build_decisions_tab(self) -> QWidget:
        wrap = QWidget()
        layout = QVBoxLayout()
        layout.setContentsMargins(0, 8, 0, 0)
        layout.setSpacing(8)
        self.decisions_table = QTableWidget(0, 6)
        self.decisions_table.setHorizontalHeaderLabels([
            "Время", "Тикер", "Решение", "Уверенность", "Риск", "Обоснование",
        ])
        self._configure_table(self.decisions_table)
        self.decisions_table.horizontalHeader().setSectionResizeMode(5, QHeaderView.Stretch)
        layout.addWidget(self.decisions_table)
        wrap.setLayout(layout)
        return wrap

    def _build_closed_tab(self) -> QWidget:
        wrap = QWidget()
        layout = QVBoxLayout()
        layout.setContentsMargins(0, 8, 0, 0)
        layout.setSpacing(8)
        self.closed_table = QTableWidget(0, 8)
        self.closed_table.setHorizontalHeaderLabels([
            "Тикер", "Кол-во", "Вход", "Закрытие", "P&L ₽", "P&L %", "Причина", "Закрыто",
        ])
        self._configure_table(self.closed_table)
        layout.addWidget(self.closed_table)
        wrap.setLayout(layout)
        return wrap

    def _build_lessons_tab(self) -> QWidget:
        wrap = QWidget()
        layout = QVBoxLayout()
        layout.setContentsMargins(0, 8, 0, 0)
        layout.setSpacing(8)
        self.lessons_table = QTableWidget(0, 5)
        self.lessons_table.setHorizontalHeaderLabels([
            "Паттерн", "Вес", "Замечен раз", "Заголовок", "Последний раз",
        ])
        self._configure_table(self.lessons_table)
        self.lessons_table.horizontalHeader().setSectionResizeMode(3, QHeaderView.Stretch)
        layout.addWidget(self.lessons_table)
        wrap.setLayout(layout)
        return wrap

    def _build_last_tick_tab(self) -> QWidget:
        wrap = QWidget()
        layout = QVBoxLayout()
        layout.setContentsMargins(0, 8, 0, 0)
        layout.setSpacing(8)
        self.tick_text = QTextEdit()
        self.tick_text.setReadOnly(True)
        self.tick_text.setStyleSheet(
            f"QTextEdit {{ background: {COLORS['panel']}; border: 1px solid {COLORS['border']}; "
            f"border-radius: 8px; padding: 10px; color: {COLORS['text']}; }}"
        )
        self.tick_text.setPlaceholderText("Запустите цикл, чтобы увидеть полный отчёт.")
        layout.addWidget(self.tick_text)
        wrap.setLayout(layout)
        return wrap

    def _configure_table(self, table: QTableWidget) -> None:
        table.setSelectionBehavior(QTableWidget.SelectRows)
        table.setEditTriggers(QTableWidget.NoEditTriggers)
        table.setAlternatingRowColors(True)
        table.verticalHeader().setVisible(False)
        table.horizontalHeader().setStretchLastSection(True)
        table.setStyleSheet(
            f"QTableWidget {{ background: {COLORS['panel']}; border: 1px solid {COLORS['border']}; "
            f"gridline-color: {COLORS['border']}; }}"
            f"QHeaderView::section {{ background: {COLORS['panel_alt']}; color: {COLORS['muted']}; "
            f"border: none; padding: 6px 8px; font-weight: 600; }}"
        )

    # ── Действия ─────────────────────────────────────────────────────────────
    def run_tick(self) -> None:
        if self._worker is not None and self._worker.isRunning():
            return
        self.status_lbl.setText("Анализирую…")
        self.status_lbl.setStyleSheet(
            f"color: {COLORS['accent_ink']}; background: {COLORS['accent']}; "
            f"border: 1px solid {COLORS['accent']}; border-radius: 8px; padding: 4px 10px;"
        )
        self.progress.setVisible(True)
        self.tick_btn.setEnabled(False)
        self._worker = TraderAgentWorker(self.agent)
        self._worker.tick_finished.connect(self._on_tick_finished)
        self._worker.error_occurred.connect(self._on_tick_error)
        self._worker.start()

    def _toggle_autorun(self, enabled: bool) -> None:
        self._autorun = enabled
        if enabled:
            self.auto_btn.setText("Авто-режим: вкл (10–30 мин)")
            self._countdown_timer.start()
            # Сразу запускаем первый тик и обновление новостей, дальше — по таймерам
            self.run_tick()
            self.refresh_news()
            self._reschedule_tick()
            self._reschedule_news()
        else:
            self.auto_btn.setText("Авто-режим: выкл")
            self._tick_timer.stop()
            self._news_timer.stop()
            self._countdown_timer.stop()
            self._next_tick_at = None
            self._next_news_at = None
            self.next_tick_lbl.setText("След. проверка позиций: —")
            self.next_news_lbl.setText("След. обновление новостей: —")

    # ── Планирование ────────────────────────────────────────────────────────
    def _reschedule_tick(self) -> None:
        if not self._autorun:
            return
        delay_ms = _random_interval_ms()
        self._tick_timer.start(delay_ms)
        self._next_tick_at = datetime.now() + timedelta(milliseconds=delay_ms)
        self._update_countdown_labels()

    def _reschedule_news(self) -> None:
        if not self._autorun:
            return
        delay_ms = _random_interval_ms()
        self._news_timer.start(delay_ms)
        self._next_news_at = datetime.now() + timedelta(milliseconds=delay_ms)
        self._update_countdown_labels()

    def _scheduled_tick(self) -> None:
        # Срабатывает по рандомному таймеру в авто-режиме.
        if not self._autorun:
            return
        self.run_tick()
        self._reschedule_tick()

    def _scheduled_news_refresh(self) -> None:
        if not self._autorun:
            return
        self.refresh_news()
        self._reschedule_news()

    def _update_countdown_labels(self) -> None:
        now = datetime.now()
        if self._next_tick_at:
            left = (self._next_tick_at - now).total_seconds()
            left = max(0, int(left))
            mm, ss = divmod(left, 60)
            self.next_tick_lbl.setText(
                f"След. проверка позиций: через {mm:02d}:{ss:02d} "
                f"(в {self._next_tick_at.strftime('%H:%M:%S')})"
            )
        if self._next_news_at:
            left = (self._next_news_at - now).total_seconds()
            left = max(0, int(left))
            mm, ss = divmod(left, 60)
            self.next_news_lbl.setText(
                f"След. обновление новостей: через {mm:02d}:{ss:02d} "
                f"(в {self._next_news_at.strftime('%H:%M:%S')})"
            )

    # ── Обновление новостей ──────────────────────────────────────────────────
    def refresh_news(self) -> None:
        if self._news_worker is not None and self._news_worker.isRunning():
            return
        self.refresh_news_btn.setEnabled(False)
        self._set_status("Качаю новости…", COLORS["accent_ink"], COLORS["accent"])
        self._news_worker = NewsRefreshWorker()
        self._news_worker.finished_with_count.connect(self._on_news_refreshed)
        self._news_worker.error_occurred.connect(self._on_news_error)
        self._news_worker.start()

    def _on_news_refreshed(self, count: int) -> None:
        self.refresh_news_btn.setEnabled(True)
        ts = datetime.now().strftime("%H:%M:%S")
        self._set_status(
            f"Новости обновлены: +{count} ({ts})",
            COLORS["success"], COLORS["success_bg"],
        )

    def _on_news_error(self, message: str) -> None:
        self.refresh_news_btn.setEnabled(True)
        self._set_status(f"Новости: ошибка ({message[:60]})", "white", COLORS["danger"])

    def _set_status(self, text: str, color: str, bg: str) -> None:
        self.status_lbl.setText(text)
        self.status_lbl.setStyleSheet(
            f"color: {color}; background: {bg}; "
            f"border: 1px solid {bg}; border-radius: 8px; padding: 4px 10px;"
        )

    def _reset_state(self) -> None:
        self.agent.db.reset()
        self.refresh_state()
        self.tick_text.setPlainText("Состояние агента сброшено. Капитал восстановлен.")

    def _on_tick_finished(self, report: AgentTickReport) -> None:
        self.progress.setVisible(False)
        self.tick_btn.setEnabled(True)
        if report.error:
            self.status_lbl.setText(f"Ошибка: {report.error[:60]}")
            self.status_lbl.setStyleSheet(
                f"color: white; background: {COLORS['danger']}; "
                f"border: 1px solid {COLORS['danger']}; border-radius: 8px; padding: 4px 10px;"
            )
        else:
            self.status_lbl.setText(
                f"Готов · {datetime.now().strftime('%H:%M:%S')}"
            )
            self.status_lbl.setStyleSheet(
                f"color: {COLORS['success']}; background: {COLORS['success_bg']}; "
                f"border: 1px solid {COLORS['success']}; border-radius: 8px; padding: 4px 10px;"
            )
        self._render_tick_report(report)
        self.refresh_state()

    def _on_tick_error(self, message: str) -> None:
        self.progress.setVisible(False)
        self.tick_btn.setEnabled(True)
        self.status_lbl.setText("Ошибка")
        self.status_lbl.setStyleSheet(
            f"color: white; background: {COLORS['danger']}; "
            f"border: 1px solid {COLORS['danger']}; border-radius: 8px; padding: 4px 10px;"
        )
        self.tick_text.setPlainText(message)

    # ── Рендеринг ────────────────────────────────────────────────────────────
    def refresh_state(self) -> None:
        prices = {p["ticker"]: p["entry_price"] for p in self.agent.portfolio.open_positions()}
        summary = self.agent.portfolio.summary(prices)
        self.stat_equity.set_value(_fmt_money(summary["equity"]))
        self.stat_cash.set_value(_fmt_money(summary["cash"]))
        self.stat_invested.set_value(_fmt_money(summary["invested"]))

        ret = summary["total_return_pct"]
        color = COLORS["success"] if ret >= 0 else COLORS["danger"]
        self.stat_return.set_value(_fmt_pct(ret), color=color)
        self.stat_open.set_value(str(summary["open_positions"]))
        self.stat_winrate.set_value(f"{summary['winrate']*100:.1f}%")
        pnl = summary["realized_pnl"]
        pnl_color = COLORS["success"] if pnl >= 0 else COLORS["danger"]
        self.stat_realized.set_value(_fmt_money(pnl), color=pnl_color)
        self.stat_closed.set_value(str(summary["closed_trades"]))

        self._fill_open_trades()
        self._fill_decisions()
        self._fill_closed()
        self._fill_lessons()

    def _fill_open_trades(self) -> None:
        rows = self.agent.db.open_trades()
        self.open_table.setRowCount(len(rows))
        for r, pos in enumerate(rows):
            cells = [
                pos["ticker"],
                str(pos["qty"]),
                f"{pos['entry_price']:.2f}",
                f"{pos['stop_loss']:.2f}",
                f"{pos['take_profit']:.2f}",
                pos["opened_at"],
                pos["deadline_at"],
                f"{pos['horizon_hours']:.1f}",
            ]
            for c, val in enumerate(cells):
                self.open_table.setItem(r, c, QTableWidgetItem(val))

    def _fill_decisions(self) -> None:
        rows = self.agent.db.recent_decisions(80)
        self.decisions_table.setRowCount(len(rows))
        for r, d in enumerate(rows):
            cells = [
                d.get("created_at", ""),
                d.get("ticker") or "—",
                d.get("action", ""),
                f"{(d.get('confidence') or 0):.2f}",
                f"{(d.get('risk_score') or 0):.2f}",
                d.get("rationale") or "",
            ]
            for c, val in enumerate(cells):
                item = QTableWidgetItem(val)
                if c == 2:
                    item.setForeground(self._action_color(val))
                self.decisions_table.setItem(r, c, item)

    @staticmethod
    def _action_color(action: str) -> QColor:
        if action == "BUY":
            return QColor(COLORS["success"])
        if action in {"SELL", "CLOSE"}:
            return QColor(COLORS["danger"])
        if action == "SKIP":
            return QColor(COLORS["muted"])
        return QColor(COLORS["text"])

    def _fill_closed(self) -> None:
        rows = self.agent.db.closed_trades(80)
        self.closed_table.setRowCount(len(rows))
        for r, t in enumerate(rows):
            pnl_abs = t.get("pnl_abs") or 0
            pnl_pct = t.get("pnl_pct") or 0
            cells = [
                t["ticker"],
                str(t["qty"]),
                f"{t['entry_price']:.2f}",
                f"{(t.get('close_price') or 0):.2f}",
                f"{pnl_abs:+.2f}",
                f"{pnl_pct:+.2f}",
                t.get("close_reason") or "",
                t.get("closed_at") or "",
            ]
            for c, val in enumerate(cells):
                item = QTableWidgetItem(val)
                if c in (4, 5):
                    item.setForeground(QColor(COLORS["success"] if pnl_abs >= 0 else COLORS["danger"]))
                self.closed_table.setItem(r, c, item)

    def _fill_lessons(self) -> None:
        rows = self.agent.db.all_lessons(100)
        self.lessons_table.setRowCount(len(rows))
        for r, lesson in enumerate(rows):
            cells = [
                lesson["pattern"],
                f"{lesson['weight']:.2f}",
                str(lesson["times_seen"]),
                lesson.get("title") or "",
                lesson.get("last_seen_at") or "",
            ]
            for c, val in enumerate(cells):
                item = QTableWidgetItem(val)
                if c == 1:
                    w = float(lesson["weight"])
                    if w > 1.05:
                        item.setForeground(QColor(COLORS["success"]))
                    elif w < 0.95:
                        item.setForeground(QColor(COLORS["danger"]))
                self.lessons_table.setItem(r, c, item)

    def _render_tick_report(self, report: AgentTickReport) -> None:
        lines: List[str] = []
        lines.append(report.summary())
        lines.append("")
        lines.append("── Открыто ────────────────────────────")
        for op in report.opened:
            lines.append(
                f"BUY {op['qty']} {op['ticker']} @ {op['entry_price']:.2f} ₽ "
                f"| SL {op['stop_loss']:.2f} | TP {op['take_profit']:.2f} "
                f"| горизонт {op['horizon_hours']:.0f} ч "
                f"| conf {op['confidence']:.2f} | risk {op['risk_score']:.2f}"
            )
            lines.append(f"  ↳ {op['rationale']}")
        if not report.opened:
            lines.append("(нет открытий)")
        lines.append("")
        lines.append("── Закрыто ────────────────────────────")
        for cl in report.closed:
            lines.append(
                f"CLOSE {cl['ticker']} qty {cl['qty']} → {cl['close_price']:.2f} "
                f"| {cl['close_reason']} | P&L {cl.get('pnl_abs', 0):+.2f} ₽ "
                f"({cl.get('pnl_pct', 0):+.2f}%)"
            )
        if not report.closed:
            lines.append("(нет закрытий)")
        lines.append("")
        lines.append("── Пропуски ───────────────────────────")
        for sk in report.skipped:
            lines.append(f"SKIP {sk.get('ticker','?')}: {sk.get('reason')}")
        if not report.skipped:
            lines.append("(нет пропусков)")
        lines.append("")
        lines.append("── Контекст ───────────────────────────")
        lines.append(f"Рынок: {report.sentiment}")
        lines.append(f"Новости: {report.news}")
        if report.error:
            lines.append("")
            lines.append(f"ОШИБКА: {report.error}")

        self.tick_text.setPlainText("\n".join(lines))
