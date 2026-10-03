"""Слой SQLite для торгового агента.

Хранит:
- portfolio       — текущий капитал и кэш
- trades          — открытые/закрытые сделки (с timeline закрытия)
- decisions       — журнал каждого решения агента (для аудита и обучения)
- market_snapshot — снимок состояния рынка в момент решения
- lessons         — извлечённые уроки из ошибок и удачных сделок
"""

from __future__ import annotations

import json
import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime
from typing import Any, Dict, Iterable, List, Optional

DEFAULT_DB_PATH = os.path.join(os.path.dirname(__file__), "..", "..", "agent_state.db")


SCHEMA = """
CREATE TABLE IF NOT EXISTS portfolio (
    id              INTEGER PRIMARY KEY CHECK (id = 1),
    initial_capital REAL    NOT NULL,
    cash            REAL    NOT NULL,
    updated_at      TEXT    NOT NULL
);

CREATE TABLE IF NOT EXISTS trades (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker          TEXT    NOT NULL,
    side            TEXT    NOT NULL,           -- 'LONG' или 'SHORT' (агент пока только LONG)
    qty             INTEGER NOT NULL,
    entry_price     REAL    NOT NULL,
    stop_loss       REAL    NOT NULL,
    take_profit     REAL    NOT NULL,
    opened_at       TEXT    NOT NULL,
    deadline_at     TEXT    NOT NULL,           -- timeline принудительного закрытия
    horizon_hours   REAL    NOT NULL,           -- сколько часов агент сам себе дал
    status          TEXT    NOT NULL,           -- 'OPEN', 'CLOSED'
    close_price     REAL,
    closed_at       TEXT,
    close_reason    TEXT,                       -- 'TP', 'SL', 'TIMEOUT', 'MANUAL', 'SIGNAL_FLIP'
    pnl_abs         REAL,
    pnl_pct         REAL,
    decision_id     INTEGER,
    FOREIGN KEY (decision_id) REFERENCES decisions(id)
);

CREATE INDEX IF NOT EXISTS idx_trades_status  ON trades(status);
CREATE INDEX IF NOT EXISTS idx_trades_ticker  ON trades(ticker);

CREATE TABLE IF NOT EXISTS decisions (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at      TEXT    NOT NULL,
    ticker          TEXT,
    action          TEXT    NOT NULL,           -- 'BUY', 'SELL', 'HOLD', 'CLOSE', 'SKIP'
    confidence      REAL,                       -- 0..1
    risk_score      REAL,                       -- 0..1, чем выше — тем рискованнее
    rationale       TEXT,                       -- человекочитаемое объяснение
    factors_json    TEXT                        -- словарь факторов (technical, news, ml, currency...)
);

CREATE INDEX IF NOT EXISTS idx_decisions_ticker ON decisions(ticker);
CREATE INDEX IF NOT EXISTS idx_decisions_action ON decisions(action);

CREATE TABLE IF NOT EXISTS market_snapshots (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    decision_id     INTEGER,
    captured_at     TEXT NOT NULL,
    ticker          TEXT,
    price           REAL,
    rsi             REAL,
    macd            REAL,
    sma20           REAL,
    sma50           REAL,
    atr_pct         REAL,
    volatility      REAL,
    market_sentiment TEXT,
    news_sentiment  REAL,
    ml_prediction   REAL,
    ml_accuracy     REAL,
    raw_json        TEXT,
    FOREIGN KEY (decision_id) REFERENCES decisions(id)
);

CREATE TABLE IF NOT EXISTS lessons (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at      TEXT NOT NULL,
    trade_id        INTEGER,
    pattern         TEXT,                       -- ключ паттерна, напр. 'RSI>75_LONG_FAILED'
    title           TEXT,
    description     TEXT,
    weight          REAL DEFAULT 1.0,           -- степень доверия уроку
    times_seen      INTEGER DEFAULT 1,
    last_seen_at    TEXT,
    FOREIGN KEY (trade_id) REFERENCES trades(id)
);

CREATE INDEX IF NOT EXISTS idx_lessons_pattern ON lessons(pattern);

CREATE TABLE IF NOT EXISTS subscribers (
    chat_id     INTEGER PRIMARY KEY,
    username    TEXT,
    first_name  TEXT,
    added_at    TEXT NOT NULL,
    is_admin    INTEGER NOT NULL DEFAULT 1,
    enabled     INTEGER NOT NULL DEFAULT 1
);
"""


def _now() -> str:
    return datetime.utcnow().isoformat(timespec="seconds")


class AgentDB:
    """Тонкая обёртка над sqlite3 с удобными методами для агента."""

    def __init__(self, path: str = DEFAULT_DB_PATH, initial_capital: float = 1_000_000.0):
        self.path = os.path.abspath(path)
        self.initial_capital = initial_capital
        self._ensure_schema()
        self._ensure_portfolio()

    # ── Инфраструктура ────────────────────────────────────────────────────────
    @contextmanager
    def _conn(self):
        conn = sqlite3.connect(self.path, timeout=10, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("PRAGMA foreign_keys=ON;")
        try:
            yield conn
        finally:
            conn.close()

    def _ensure_schema(self) -> None:
        with self._conn() as c:
            c.executescript(SCHEMA)

    def _ensure_portfolio(self) -> None:
        with self._conn() as c:
            row = c.execute("SELECT id FROM portfolio WHERE id = 1").fetchone()
            if row is None:
                c.execute(
                    "INSERT INTO portfolio (id, initial_capital, cash, updated_at) "
                    "VALUES (1, ?, ?, ?)",
                    (self.initial_capital, self.initial_capital, _now()),
                )

    # ── Portfolio ─────────────────────────────────────────────────────────────
    def get_portfolio(self) -> Dict[str, Any]:
        with self._conn() as c:
            row = c.execute(
                "SELECT initial_capital, cash, updated_at FROM portfolio WHERE id = 1"
            ).fetchone()
            return dict(row) if row else {}

    def update_cash(self, cash: float) -> None:
        with self._conn() as c:
            c.execute(
                "UPDATE portfolio SET cash = ?, updated_at = ? WHERE id = 1",
                (cash, _now()),
            )

    def reset(self) -> None:
        """Полный сброс — для отладки."""
        with self._conn() as c:
            for table in ("lessons", "market_snapshots", "decisions", "trades"):
                c.execute(f"DELETE FROM {table}")
            c.execute(
                "UPDATE portfolio SET cash = initial_capital, updated_at = ? WHERE id = 1",
                (_now(),),
            )

    # ── Decisions ─────────────────────────────────────────────────────────────
    def log_decision(
        self,
        *,
        ticker: Optional[str],
        action: str,
        confidence: float,
        risk_score: float,
        rationale: str,
        factors: Dict[str, Any],
    ) -> int:
        with self._conn() as c:
            cur = c.execute(
                "INSERT INTO decisions (created_at, ticker, action, confidence, risk_score, "
                "rationale, factors_json) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    _now(),
                    ticker,
                    action,
                    float(confidence),
                    float(risk_score),
                    rationale,
                    json.dumps(factors, ensure_ascii=False, default=str),
                ),
            )
            return int(cur.lastrowid)

    def recent_decisions(self, limit: int = 50) -> List[Dict[str, Any]]:
        with self._conn() as c:
            rows = c.execute(
                "SELECT * FROM decisions ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()
            return [dict(r) for r in rows]

    # ── Snapshots ─────────────────────────────────────────────────────────────
    def save_snapshot(self, decision_id: int, ticker: str, snapshot: Dict[str, Any]) -> None:
        with self._conn() as c:
            c.execute(
                "INSERT INTO market_snapshots (decision_id, captured_at, ticker, price, rsi, "
                "macd, sma20, sma50, atr_pct, volatility, market_sentiment, news_sentiment, "
                "ml_prediction, ml_accuracy, raw_json) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    decision_id,
                    _now(),
                    ticker,
                    snapshot.get("price"),
                    snapshot.get("rsi"),
                    snapshot.get("macd"),
                    snapshot.get("sma20"),
                    snapshot.get("sma50"),
                    snapshot.get("atr_pct"),
                    snapshot.get("volatility"),
                    snapshot.get("market_sentiment"),
                    snapshot.get("news_sentiment"),
                    snapshot.get("ml_prediction"),
                    snapshot.get("ml_accuracy"),
                    json.dumps(snapshot, ensure_ascii=False, default=str),
                ),
            )

    # ── Trades ────────────────────────────────────────────────────────────────
    def open_trade(
        self,
        *,
        ticker: str,
        side: str,
        qty: int,
        entry_price: float,
        stop_loss: float,
        take_profit: float,
        horizon_hours: float,
        deadline_at: str,
        decision_id: Optional[int] = None,
    ) -> int:
        with self._conn() as c:
            cur = c.execute(
                "INSERT INTO trades (ticker, side, qty, entry_price, stop_loss, take_profit, "
                "opened_at, deadline_at, horizon_hours, status, decision_id) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'OPEN', ?)",
                (
                    ticker,
                    side,
                    int(qty),
                    float(entry_price),
                    float(stop_loss),
                    float(take_profit),
                    _now(),
                    deadline_at,
                    float(horizon_hours),
                    decision_id,
                ),
            )
            return int(cur.lastrowid)

    def close_trade(
        self,
        trade_id: int,
        *,
        close_price: float,
        reason: str,
        commission_pct: float = 0.0,
    ) -> Dict[str, Any]:
        """P&L считается за вычетом комиссии на вход и выход — иначе сделка
        с плюсом меньше комиссии числилась бы прибыльной."""
        with self._conn() as c:
            row = c.execute("SELECT * FROM trades WHERE id = ?", (trade_id,)).fetchone()
            if row is None:
                return {}
            trade = dict(row)
            if trade["side"] == "LONG":
                pnl_abs = (close_price - trade["entry_price"]) * trade["qty"]
            else:
                pnl_abs = (trade["entry_price"] - close_price) * trade["qty"]
            pnl_abs -= (trade["entry_price"] + close_price) * trade["qty"] * commission_pct / 100.0
            pnl_pct = pnl_abs / (trade["entry_price"] * trade["qty"]) * 100.0
            c.execute(
                "UPDATE trades SET status='CLOSED', close_price=?, closed_at=?, "
                "close_reason=?, pnl_abs=?, pnl_pct=? WHERE id=?",
                (float(close_price), _now(), reason, pnl_abs, pnl_pct, trade_id),
            )
            trade.update(
                status="CLOSED",
                close_price=close_price,
                closed_at=_now(),
                close_reason=reason,
                pnl_abs=pnl_abs,
                pnl_pct=pnl_pct,
            )
            return trade

    def open_trades(self) -> List[Dict[str, Any]]:
        with self._conn() as c:
            rows = c.execute(
                "SELECT * FROM trades WHERE status = 'OPEN' ORDER BY opened_at"
            ).fetchall()
            return [dict(r) for r in rows]

    def closed_trades(self, limit: int = 100) -> List[Dict[str, Any]]:
        with self._conn() as c:
            rows = c.execute(
                "SELECT * FROM trades WHERE status = 'CLOSED' ORDER BY closed_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
            return [dict(r) for r in rows]

    def open_trade_for_ticker(self, ticker: str) -> Optional[Dict[str, Any]]:
        with self._conn() as c:
            row = c.execute(
                "SELECT * FROM trades WHERE status='OPEN' AND ticker=? LIMIT 1",
                (ticker,),
            ).fetchone()
            return dict(row) if row else None

    # ── Lessons ───────────────────────────────────────────────────────────────
    def upsert_lesson(
        self,
        *,
        pattern: str,
        title: str,
        description: str,
        trade_id: Optional[int] = None,
        weight_delta: float = 0.0,
    ) -> None:
        with self._conn() as c:
            row = c.execute(
                "SELECT id, weight, times_seen FROM lessons WHERE pattern = ?", (pattern,)
            ).fetchone()
            if row is None:
                c.execute(
                    "INSERT INTO lessons (created_at, trade_id, pattern, title, description, "
                    "weight, times_seen, last_seen_at) VALUES (?, ?, ?, ?, ?, ?, 1, ?)",
                    (_now(), trade_id, pattern, title, description, 1.0 + weight_delta, _now()),
                )
            else:
                c.execute(
                    "UPDATE lessons SET weight = ?, times_seen = ?, last_seen_at = ?, "
                    "description = ? WHERE id = ?",
                    (
                        float(row["weight"]) + weight_delta,
                        int(row["times_seen"]) + 1,
                        _now(),
                        description,
                        row["id"],
                    ),
                )

    def lessons_for(self, patterns: Iterable[str]) -> List[Dict[str, Any]]:
        patterns = list(patterns)
        if not patterns:
            return []
        placeholders = ",".join("?" * len(patterns))
        with self._conn() as c:
            rows = c.execute(
                f"SELECT * FROM lessons WHERE pattern IN ({placeholders})", patterns
            ).fetchall()
            return [dict(r) for r in rows]

    def all_lessons(self, limit: int = 100) -> List[Dict[str, Any]]:
        with self._conn() as c:
            rows = c.execute(
                "SELECT * FROM lessons ORDER BY weight DESC, times_seen DESC LIMIT ?",
                (limit,),
            ).fetchall()
            return [dict(r) for r in rows]

    # ── Подписчики Telegram ──────────────────────────────────────────────────
    def add_subscriber(
        self,
        chat_id: int,
        username: Optional[str] = None,
        first_name: Optional[str] = None,
        is_admin: bool = True,
    ) -> None:
        with self._conn() as c:
            row = c.execute(
                "SELECT chat_id FROM subscribers WHERE chat_id = ?", (chat_id,)
            ).fetchone()
            if row is None:
                c.execute(
                    "INSERT INTO subscribers (chat_id, username, first_name, added_at, is_admin, enabled) "
                    "VALUES (?, ?, ?, ?, ?, 1)",
                    (chat_id, username, first_name, _now(), int(bool(is_admin))),
                )
            else:
                c.execute(
                    "UPDATE subscribers SET username = ?, first_name = ?, enabled = 1 "
                    "WHERE chat_id = ?",
                    (username, first_name, chat_id),
                )

    def disable_subscriber(self, chat_id: int) -> None:
        with self._conn() as c:
            c.execute("UPDATE subscribers SET enabled = 0 WHERE chat_id = ?", (chat_id,))

    def active_subscribers(self) -> List[int]:
        with self._conn() as c:
            rows = c.execute(
                "SELECT chat_id FROM subscribers WHERE enabled = 1"
            ).fetchall()
            return [int(r["chat_id"]) for r in rows]

    def is_subscriber(self, chat_id: int) -> bool:
        with self._conn() as c:
            row = c.execute(
                "SELECT 1 FROM subscribers WHERE chat_id = ? AND enabled = 1", (chat_id,)
            ).fetchone()
            return row is not None

    # ── Аналитика ────────────────────────────────────────────────────────────
    def stats(self) -> Dict[str, Any]:
        with self._conn() as c:
            total = c.execute("SELECT COUNT(*) FROM trades WHERE status='CLOSED'").fetchone()[0]
            wins = c.execute(
                "SELECT COUNT(*) FROM trades WHERE status='CLOSED' AND pnl_abs > 0"
            ).fetchone()[0]
            pnl = c.execute(
                "SELECT COALESCE(SUM(pnl_abs), 0) FROM trades WHERE status='CLOSED'"
            ).fetchone()[0]
            open_cnt = c.execute("SELECT COUNT(*) FROM trades WHERE status='OPEN'").fetchone()[0]
        return {
            "closed_total": int(total or 0),
            "wins": int(wins or 0),
            "winrate": (wins / total) if total else 0.0,
            "realized_pnl": float(pnl or 0.0),
            "open_positions": int(open_cnt or 0),
        }
