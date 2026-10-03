"""Риск-менеджмент: позиционирование, SL/TP, проверка лимитов."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Optional

from app.agent.portfolio import Portfolio


@dataclass
class RiskParams:
    risk_per_trade_pct: float = 1.0       # % от капитала на одну сделку
    max_position_pct: float = 15.0        # % капитала в одну бумагу
    max_total_exposure_pct: float = 80.0  # % капитала суммарно
    atr_stop_mult: float = 2.0            # SL = ATR * mult
    atr_take_mult: float = 3.0            # TP = ATR * mult (R:R = take/stop)
    min_stop_pct: float = 1.5             # минимум 1.5% движения для SL
    max_stop_pct: float = 8.0             # максимум 8% — иначе бумага слишком волатильна
    commission_pct: float = 0.05
    political_risk_cutoff: float = 0.5    # выше — режем размер позиции
    min_fill_ratio: float = 1 / 3         # урезанная под остаток кэша/лимита позиция меньше этой доли обычной — не входим
    max_open_positions: int = 8


@dataclass
class TradePlan:
    ticker: str
    qty: int
    entry_price: float
    stop_loss: float
    take_profit: float
    risk_amount: float          # сколько ₽ под риском
    risk_reward: float
    sized_by: str               # 'ATR' / 'PCT'
    side: str = "LONG"          # 'LONG' / 'SHORT'


class RiskManager:
    def __init__(self, portfolio: Portfolio, params: Optional[RiskParams] = None):
        self.portfolio = portfolio
        self.p = params or RiskParams()

    # ── Размер позиции ────────────────────────────────────────────────────────
    def plan_trade(
        self,
        ticker: str,
        price: float,
        atr_pct: Optional[float],
        political_risk: float = 0.0,
        confidence: float = 0.5,
        side: str = "LONG",
    ) -> Optional[TradePlan]:
        """Размер и уровни сделки. Для SHORT стоп выше цены входа, тейк — ниже;
        под шорт резервируется обеспечение на ту же сумму, что и под лонг."""
        if price <= 0:
            return None

        cap = self.portfolio.initial_capital()
        if cap <= 0:
            return None

        # 1. Stop по ATR, но в безопасных рамках
        if atr_pct and atr_pct > 0:
            stop_pct = atr_pct * self.p.atr_stop_mult
            sized_by = "ATR"
        else:
            stop_pct = 3.0
            sized_by = "PCT"
        stop_pct = max(self.p.min_stop_pct, min(stop_pct, self.p.max_stop_pct))

        take_pct = stop_pct * (self.p.atr_take_mult / self.p.atr_stop_mult)

        if side == "SHORT":
            stop_loss = price * (1 + stop_pct / 100.0)
            take_profit = price * (1 - take_pct / 100.0)
        else:
            stop_loss = price * (1 - stop_pct / 100.0)
            take_profit = price * (1 + take_pct / 100.0)

        # 2. Размер сделки в ₽
        risk_pct = self.p.risk_per_trade_pct
        # модулируем уверенностью и политическим риском
        risk_pct *= max(0.3, min(1.5, confidence * 1.5))
        if political_risk > self.p.political_risk_cutoff:
            risk_pct *= 0.5

        risk_amount = cap * risk_pct / 100.0
        per_share_risk = abs(price - stop_loss)
        if per_share_risk <= 0:
            return None

        qty = int(risk_amount // per_share_risk)
        if qty <= 0:
            return None

        # 3. Ограничение на размер позиции в % капитала
        max_pos_value = cap * self.p.max_position_pct / 100.0
        max_qty_by_position = int(max_pos_value // price)
        qty = min(qty, max_qty_by_position)
        if qty <= 0:
            return None
        normal_qty = qty  # столько взяли бы при свободном кэше и лимите экспозиции

        # 4. Кэш и общее экспозиции
        if not self.portfolio.can_afford(qty, price, self.p.commission_pct):
            affordable = int(self.portfolio.cash() // (price * (1 + self.p.commission_pct / 100.0)))
            qty = min(qty, affordable)
        if qty <= 0:
            return None

        # 5. Лимит суммарного объёма позиций
        prices = {pos["ticker"]: pos["entry_price"] for pos in self.portfolio.open_positions()}
        prices[ticker] = price
        projected_exposure = self.portfolio.positions_value(prices) + qty * price
        if projected_exposure / cap * 100.0 > self.p.max_total_exposure_pct:
            allowed_value = cap * self.p.max_total_exposure_pct / 100.0 - (
                projected_exposure - qty * price
            )
            qty = max(0, int(allowed_value // price))
            if qty <= 0:
                return None

        # 6. Позиция, урезанная под остаток кэша или лимита, — «пылинка» вроде
        #    5 акций на 2 ₽: заработать не может, а слот из 8 занимает.
        if qty < normal_qty * self.p.min_fill_ratio:
            return None

        risk_reward = round(take_pct / stop_pct, 2)
        actual_risk_amount = qty * per_share_risk

        return TradePlan(
            ticker=ticker,
            qty=qty,
            entry_price=round(price, 4),
            stop_loss=round(stop_loss, 4),
            take_profit=round(take_profit, 4),
            risk_amount=round(actual_risk_amount, 2),
            risk_reward=risk_reward,
            sized_by=sized_by,
            side=side,
        )

    # ── Проверки портфеля ────────────────────────────────────────────────────
    def can_open_new(self) -> tuple[bool, str]:
        open_positions = self.portfolio.open_positions()
        if len(open_positions) >= self.p.max_open_positions:
            return False, f"Лимит активных позиций ({self.p.max_open_positions}) исчерпан"
        cap = self.portfolio.initial_capital()
        prices = {p["ticker"]: p["entry_price"] for p in open_positions}
        exposure = self.portfolio.positions_value(prices) / cap * 100.0 if cap else 0.0
        if exposure >= self.p.max_total_exposure_pct:
            return False, f"Суммарная экспозиция {exposure:.1f}% ≥ лимита {self.p.max_total_exposure_pct}%"
        return True, "OK"

    def overall_risk_score(self, signal_score: float, political_risk: float, volatility: float) -> float:
        """0..1: 0 — спокойно, 1 — критически рискованно."""
        vol_component = min(1.0, max(0.0, (volatility - 20.0) / 60.0))  # норма ~20%, край ~80%
        pol_component = min(1.0, max(0.0, political_risk))
        signal_component = 1.0 - min(1.0, abs(signal_score) / 5.0)      # слабый сигнал ⇒ выше риск
        return round(0.4 * vol_component + 0.35 * pol_component + 0.25 * signal_component, 3)
