"""Учёт капитала и позиций. Лимит — 1 млн ₽ по умолчанию."""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from app.agent.database import AgentDB


class Portfolio:
    """Тонкая обёртка над AgentDB для расчёта свободного капитала и P&L."""

    def __init__(self, db: AgentDB):
        self.db = db

    # ── Состояние ────────────────────────────────────────────────────────────
    def cash(self) -> float:
        return float(self.db.get_portfolio().get("cash", 0.0))

    def initial_capital(self) -> float:
        return float(self.db.get_portfolio().get("initial_capital", 0.0))

    def open_positions(self) -> List[Dict[str, Any]]:
        return self.db.open_trades()

    def positions_value(self, prices: Dict[str, float]) -> float:
        """Сколько вернётся в кэш, если закрыть всё по этим ценам (без комиссий).
        Шорт стоит своё обеспечение плюс результат: qty × (2 × вход − цена)."""
        total = 0.0
        for pos in self.open_positions():
            ticker = pos["ticker"]
            qty = pos["qty"]
            entry = pos["entry_price"]
            price = prices.get(ticker, entry)
            total += qty * (2 * entry - price) if pos.get("side") == "SHORT" else qty * price
        return total

    def equity(self, prices: Dict[str, float]) -> float:
        return self.cash() + self.positions_value(prices)

    def exposure_pct(self, prices: Dict[str, float]) -> float:
        cap = self.initial_capital()
        if cap <= 0:
            return 0.0
        return self.positions_value(prices) / cap * 100.0

    # ── Операции ─────────────────────────────────────────────────────────────
    def can_afford(self, qty: int, price: float, commission_pct: float = 0.05) -> bool:
        cost = qty * price * (1 + commission_pct / 100.0)
        return self.cash() >= cost

    def reserve_for_buy(self, qty: int, price: float, commission_pct: float = 0.05) -> float:
        """Списать кэш под сделку: под лонг — покупка, под шорт — обеспечение
        на ту же сумму. Возвращает фактически списанную сумму."""
        cost = qty * price * (1 + commission_pct / 100.0)
        new_cash = self.cash() - cost
        self.db.update_cash(new_cash)
        return cost

    def release_on_close(
        self, position: Dict[str, Any], price: float, commission_pct: float = 0.05
    ) -> float:
        """Зачислить кэш при закрытии позиции. Возвращает зачисленную сумму."""
        qty, entry = position["qty"], position["entry_price"]
        if position.get("side") == "SHORT":
            # Возвращаем обеспечение (qty × вход) плюс результат шорта (вход − цена)
            gross = qty * (2 * entry - price)
        else:
            gross = qty * price
        proceeds = gross - qty * price * commission_pct / 100.0
        self.db.update_cash(self.cash() + proceeds)
        return proceeds

    # ── Доход на свободных деньгах ───────────────────────────────────────────
    def accrue_cash_yield(self, fund_price: float, trade_date: str) -> float:
        """Свободный кэш «лежит» в фонде денежного рынка (LQDT): раз в торговый
        день он растёт так же, как цена фонда. Возвращает начисление в рублях."""
        last_date = self.db.get_kv("cash_fund_date")
        last_price = float(self.db.get_kv("cash_fund_price", "0") or 0)
        if last_date == trade_date:
            return 0.0
        accrued = 0.0
        cash = self.cash()
        if last_price > 0 and cash > 0:
            accrued = cash * (fund_price / last_price - 1)
            self.db.update_cash(cash + accrued)
            total = float(self.db.get_kv("cash_yield_total", "0") or 0)
            self.db.set_kv("cash_yield_total", round(total + accrued, 2))
        self.db.set_kv("cash_fund_price", fund_price)
        self.db.set_kv("cash_fund_date", trade_date)
        return accrued

    def cash_yield_total(self) -> float:
        return float(self.db.get_kv("cash_yield_total", "0") or 0)

    # ── Сводка ───────────────────────────────────────────────────────────────
    def summary(self, prices: Optional[Dict[str, float]] = None) -> Dict[str, Any]:
        prices = prices or {}
        cap = self.initial_capital()
        cash = self.cash()
        invested = self.positions_value(prices)
        equity = cash + invested
        stats = self.db.stats()
        return {
            "initial_capital": cap,
            "cash": cash,
            "invested": invested,
            "equity": equity,
            "total_return_pct": (equity - cap) / cap * 100.0 if cap else 0.0,
            "open_positions": stats["open_positions"],
            "closed_trades": stats["closed_total"],
            "winrate": stats["winrate"],
            "realized_pnl": stats["realized_pnl"],
        }
