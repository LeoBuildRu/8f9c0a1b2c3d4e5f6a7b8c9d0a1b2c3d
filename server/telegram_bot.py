"""Telegram-бот: команды + пуш-уведомления о сделках.

Использует python-telegram-bot v20+ (async API).
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from typing import Any, Dict, List, Optional

from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CommandHandler,
    ContextTypes,
)
from telegram.request import HTTPXRequest

from app.agent.database import AgentDB
from app.agent.portfolio import Portfolio
from app.agent.trader_agent import TraderAgent
from server import config

log = logging.getLogger(__name__)


def _md_escape(text: str) -> str:
    """Минимальный экранизатор для MarkdownV2."""
    if text is None:
        return ""
    for ch in r"_*[]()~`>#+-=|{}.!":
        text = text.replace(ch, "\\" + ch)
    return text


def _fmt_money(value: float) -> str:
    return f"{value:,.2f}".replace(",", " ") + " ₽"


def _fmt_pct(value: float) -> str:
    sign = "+" if value >= 0 else ""
    return f"{sign}{value:.2f}%"


class TelegramBot:
    """Тонкая обёртка над python-telegram-bot."""

    def __init__(self, token: str, agent: TraderAgent, get_prices, proxy_url: Optional[str] = None):
        if not token:
            raise RuntimeError("LEOAGENT_TG_TOKEN не задан")
        self.agent = agent
        self.db: AgentDB = agent.db
        self.portfolio: Portfolio = agent.portfolio
        self.get_prices = get_prices  # callable -> Dict[str, float]
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self.app: Application = self._build_app(token, proxy_url)
        self._register_handlers()

        # Прокидываем колбэки агента в bot
        self.agent.on_trade_opened = self._on_trade_opened
        self.agent.on_trade_closed = self._on_trade_closed
        self.agent.on_tick_done = self._on_tick_done

    # ── Сборка приложения с (опциональным) прокси ────────────────────────────
    @staticmethod
    def _build_app(token: str, proxy_url: Optional[str]) -> Application:
        builder: ApplicationBuilder = Application.builder().token(token)
        if proxy_url:
            log.info("Telegram идёт через прокси: %s", proxy_url.split("@")[-1])
            # Два независимых httpx-клиента: один для отправки сообщений,
            # второй для long-polling getUpdates.
            try:
                request = HTTPXRequest(
                    proxy=proxy_url,
                    connect_timeout=20.0,
                    read_timeout=30.0,
                    write_timeout=20.0,
                )
                updates_request = HTTPXRequest(
                    proxy=proxy_url,
                    connect_timeout=20.0,
                    read_timeout=60.0,
                    write_timeout=20.0,
                )
                builder = builder.request(request).get_updates_request(updates_request)
            except TypeError:
                # На очень старых версиях PTB параметр назывался proxy_url
                request = HTTPXRequest(proxy_url=proxy_url)
                updates_request = HTTPXRequest(proxy_url=proxy_url)
                builder = builder.request(request).get_updates_request(updates_request)
        return builder.build()

    # ── Регистрация ──────────────────────────────────────────────────────────
    def _register_handlers(self) -> None:
        self.app.add_handler(CommandHandler("start", self.cmd_start))
        self.app.add_handler(CommandHandler("help", self.cmd_help))
        self.app.add_handler(CommandHandler("status", self.cmd_status))
        self.app.add_handler(CommandHandler("portfolio", self.cmd_portfolio))
        self.app.add_handler(CommandHandler("positions", self.cmd_positions))
        self.app.add_handler(CommandHandler("history", self.cmd_history))
        self.app.add_handler(CommandHandler("decisions", self.cmd_decisions))
        self.app.add_handler(CommandHandler("lessons", self.cmd_lessons))
        self.app.add_handler(CommandHandler("stop", self.cmd_stop))

    # ── Запуск / останов ─────────────────────────────────────────────────────
    async def start(self) -> None:
        # Колбэки агента приходят из рабочего потока (asyncio.to_thread),
        # где своего event loop нет — отправляем сообщения в этот.
        self._loop = asyncio.get_running_loop()
        await self.app.initialize()
        await self.app.start()
        await self.app.updater.start_polling(
            allowed_updates=Update.ALL_TYPES,
            drop_pending_updates=True,
        )
        log.info("Telegram polling запущен")

    async def shutdown(self) -> None:
        try:
            await self.app.updater.stop()
        except Exception:
            pass
        try:
            await self.app.stop()
        except Exception:
            pass
        await self.app.shutdown()

    # ── Authorization ────────────────────────────────────────────────────────
    def _is_authorized(self, chat_id: int) -> bool:
        if config.ADMIN_CHAT_IDS:
            return chat_id in config.ADMIN_CHAT_IDS
        # Если allow-list пуст — любой, кто прошёл /start, считается доверенным.
        return self.db.is_subscriber(chat_id)

    async def _deny(self, update: Update) -> None:
        if update.effective_chat:
            await update.effective_chat.send_message(
                "У вас нет доступа к этому боту."
            )

    # ── Команды ──────────────────────────────────────────────────────────────
    async def cmd_start(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        chat = update.effective_chat
        user = update.effective_user
        if chat is None or user is None:
            return

        if config.ADMIN_CHAT_IDS and chat.id not in config.ADMIN_CHAT_IDS:
            await chat.send_message("Этот бот закрытый — обратитесь к администратору.")
            return

        self.db.add_subscriber(
            chat_id=chat.id,
            username=user.username,
            first_name=user.first_name,
            is_admin=True,
        )
        await chat.send_message(
            "Привет! Я — AI-агент трейдинга MOEX.\n\n"
            "Я работаю с капиталом и буду сообщать о каждом своём действии.\n\n"
            "Команды:\n"
            "/status — общий статус (рынок + новости)\n"
            "/portfolio — капитал, кэш, доходность\n"
            "/positions — текущие позиции с P&L\n"
            "/history — последние закрытые сделки\n"
            "/decisions — журнал решений\n"
            "/lessons — что я выучил\n"
            "/stop — отписаться от уведомлений\n"
        )

    async def cmd_help(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        await self.cmd_start(update, context)

    async def cmd_stop(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        chat = update.effective_chat
        if chat is None:
            return
        self.db.disable_subscriber(chat.id)
        await chat.send_message("Уведомления отключены. Возвращайтесь через /start.")

    async def cmd_status(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._guard(update):
            return
        prices = self.get_prices() if self.get_prices else {}
        summary = self.portfolio.summary(prices)
        text = (
            "📊 *Статус агента*\n\n"
            f"Капитал: `{_fmt_money(summary['equity'])}`\n"
            f"Свободно: `{_fmt_money(summary['cash'])}`\n"
            f"В позициях: `{_fmt_money(summary['invested'])}`\n"
            f"Доходность: *{_fmt_pct(summary['total_return_pct'])}*\n"
            f"Открыто: {summary['open_positions']}, "
            f"закрыто: {summary['closed_trades']}, "
            f"winrate: {summary['winrate']*100:.1f}%\n"
        )
        await update.effective_chat.send_message(_md_escape(text), parse_mode=ParseMode.MARKDOWN_V2)

    async def cmd_portfolio(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._guard(update):
            return
        prices = self.get_prices() if self.get_prices else {}
        s = self.portfolio.summary(prices)
        pnl_color = "🟢" if s["realized_pnl"] >= 0 else "🔴"
        text = (
            "💼 *Портфель*\n\n"
            f"Старт: {_fmt_money(s['initial_capital'])}\n"
            f"Сейчас: *{_fmt_money(s['equity'])}* ({_fmt_pct(s['total_return_pct'])})\n"
            f"Кэш: {_fmt_money(s['cash'])}\n"
            f"В рынке: {_fmt_money(s['invested'])}\n\n"
            f"{pnl_color} Реализованный P&L: {_fmt_money(s['realized_pnl'])}\n"
            f"Сделок закрыто: {s['closed_trades']}, winrate: {s['winrate']*100:.1f}%\n"
        )
        await update.effective_chat.send_message(_md_escape(text), parse_mode=ParseMode.MARKDOWN_V2)

    async def cmd_positions(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._guard(update):
            return
        positions = self.db.open_trades()
        if not positions:
            await update.effective_chat.send_message("Открытых позиций нет.")
            return
        prices = self.get_prices() if self.get_prices else {}
        lines = ["📈 *Открытые позиции*\n"]
        for pos in positions:
            ticker = pos["ticker"]
            qty = pos["qty"]
            entry = pos["entry_price"]
            side = pos.get("side", "LONG")
            sign = -1 if side == "SHORT" else 1
            price_now = prices.get(ticker, entry)
            pnl_abs = (price_now - entry) * qty * sign
            pnl_pct = (price_now - entry) / entry * 100.0 * sign if entry else 0.0
            mark = "🟢" if pnl_abs >= 0 else "🔴"
            deadline = pos.get("deadline_at", "")
            lines.append(
                f"{mark} *{ticker}* {side} x{qty}\n"
                f"  вход {entry:.2f} → сейчас {price_now:.2f}\n"
                f"  P&L: {_fmt_money(pnl_abs)} ({_fmt_pct(pnl_pct)})\n"
                f"  SL {pos['stop_loss']:.2f} | TP {pos['take_profit']:.2f}\n"
                f"  дедлайн {deadline}\n"
            )
        await update.effective_chat.send_message(
            _md_escape("\n".join(lines)), parse_mode=ParseMode.MARKDOWN_V2
        )

    async def cmd_history(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._guard(update):
            return
        trades = self.db.closed_trades(15)
        if not trades:
            await update.effective_chat.send_message("Закрытых сделок ещё нет.")
            return
        lines = ["📜 *Последние закрытые сделки*\n"]
        for t in trades:
            pnl_abs = t.get("pnl_abs") or 0
            pnl_pct = t.get("pnl_pct") or 0
            mark = "🟢" if pnl_abs >= 0 else "🔴"
            lines.append(
                f"{mark} {t['ticker']} {t.get('side', 'LONG')} x{t['qty']} — "
                f"{_fmt_money(pnl_abs)} ({_fmt_pct(pnl_pct)}) "
                f"[{t.get('close_reason')}]"
            )
        await update.effective_chat.send_message(
            _md_escape("\n".join(lines)), parse_mode=ParseMode.MARKDOWN_V2
        )

    async def cmd_decisions(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._guard(update):
            return
        decisions = self.db.recent_decisions(10)
        if not decisions:
            await update.effective_chat.send_message("Журнал пуст.")
            return
        lines = ["🧠 *Последние решения*\n"]
        for d in decisions:
            ts = d.get("created_at", "")[:19]
            lines.append(
                f"`{ts}` {d.get('action')} {d.get('ticker') or '—'} "
                f"(c={(d.get('confidence') or 0):.2f}, r={(d.get('risk_score') or 0):.2f})\n"
                f"  {(d.get('rationale') or '')[:200]}"
            )
        await update.effective_chat.send_message(
            _md_escape("\n\n".join(lines)), parse_mode=ParseMode.MARKDOWN_V2
        )

    async def cmd_lessons(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._guard(update):
            return
        lessons = self.db.all_lessons(15)
        if not lessons:
            await update.effective_chat.send_message("Ещё ничему не научился — нет закрытых сделок.")
            return
        lines = ["🎓 *Выученные уроки*\n"]
        for l in lessons:
            weight = float(l.get("weight") or 1.0)
            mark = "🟢" if weight > 1.05 else "🔴" if weight < 0.95 else "⚪"
            lines.append(
                f"{mark} `{l['pattern']}` w={weight:.2f} "
                f"(x{l['times_seen']})\n  {(l.get('title') or '')[:100]}"
            )
        await update.effective_chat.send_message(
            _md_escape("\n".join(lines)), parse_mode=ParseMode.MARKDOWN_V2
        )

    # ── Внутренняя проверка ─────────────────────────────────────────────────
    def _guard(self, update: Update) -> bool:
        chat = update.effective_chat
        if chat is None:
            return False
        if not self._is_authorized(chat.id):
            asyncio.create_task(self._deny(update))
            return False
        return True

    # ── Уведомления о сделках (вызываются из TraderAgent) ───────────────────
    def _on_trade_opened(self, trade: Dict[str, Any]) -> None:
        text = (
            "🟢 *Открыта сделка*\n\n"
            f"*{trade['ticker']}* {trade.get('side', 'LONG')} x{trade['qty']}\n"
            f"Цена входа: {trade['entry_price']:.2f}\n"
            f"SL: {trade['stop_loss']:.2f}\n"
            f"TP: {trade['take_profit']:.2f}\n"
            f"Горизонт: {trade['horizon_hours']:.1f} ч\n"
            f"Уверенность: {trade['confidence']:.2f}, риск: {trade['risk_score']:.2f}\n\n"
            f"_{trade.get('rationale', '')[:380]}_"
        )
        self._broadcast(text)

    def _on_trade_closed(self, trade: Dict[str, Any]) -> None:
        pnl_abs = trade.get("pnl_abs") or 0
        pnl_pct = trade.get("pnl_pct") or 0
        mark = "🟢" if pnl_abs >= 0 else "🔴"
        text = (
            f"{mark} *Сделка закрыта*\n\n"
            f"*{trade['ticker']}* {trade.get('side', 'LONG')} x{trade['qty']}\n"
            f"Вход {trade['entry_price']:.2f} → выход {trade.get('close_price', 0):.2f}\n"
            f"P&L: {_fmt_money(pnl_abs)} ({_fmt_pct(pnl_pct)})\n"
            f"Причина: {trade.get('close_reason')}\n"
        )
        self._broadcast(text)

    def _on_tick_done(self, report) -> None:
        # Уведомлять про каждый тик не нужно — это шум. Шлём только если что-то открыли/закрыли.
        if not report.opened and not report.closed:
            return
        # Уведомления о сделках уже ушли в _on_trade_*. Здесь только короткая сводка.
        summary = (
            f"🔁 Тик завершён: открыто {len(report.opened)}, "
            f"закрыто {len(report.closed)}. "
            f"Рынок {report.sentiment.get('label')}, новости {report.news.get('overall_sentiment'):+.2f}."
        )
        self._broadcast(summary)

    def _broadcast(self, text: str) -> None:
        chat_ids: List[int] = list(config.ADMIN_CHAT_IDS) or self.db.active_subscribers()
        if not chat_ids:
            log.info("Нет подписчиков для рассылки: %s", text[:80])
            return
        if self._loop is None or not self._loop.is_running():
            log.warning("Бот не запущен, сообщение не отправлено: %s", text[:80])
            return
        for cid in chat_ids:
            asyncio.run_coroutine_threadsafe(self._send_safe(cid, text), self._loop)

    async def _send_safe(self, chat_id: int, text: str) -> None:
        try:
            await self.app.bot.send_message(
                chat_id=chat_id,
                text=_md_escape(text),
                parse_mode=ParseMode.MARKDOWN_V2,
            )
        except Exception as exc:
            log.warning("Не удалось отправить chat_id=%s: %s", chat_id, exc)
