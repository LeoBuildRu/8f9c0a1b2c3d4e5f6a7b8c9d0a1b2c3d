"""Обучение на закрытых сделках.

Принцип простой: каждая закрытая сделка превращается в один или несколько
паттернов (например, "RSI>70_BUY"), для которых мы храним накопленный P&L
и количество наблюдений. На вход новой сделке агент тянет уроки для тех
же паттернов и корректирует уверенность / отказывается, если паттерн
систематически приносит убыток.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Optional

from app.agent.database import AgentDB


def _bucket(value: Optional[float], thresholds: List[float], labels: List[str]) -> Optional[str]:
    if value is None:
        return None
    for t, lbl in zip(thresholds, labels):
        if value < t:
            return lbl
    return labels[-1]


def features_to_patterns(features: Dict[str, Any], action: str) -> List[str]:
    """Превращает словарь факторов в набор ключей паттернов."""
    patterns: List[str] = []
    rsi = features.get("rsi")
    macd = features.get("macd")
    macd_sig = features.get("macd_signal")
    vol = features.get("volatility_annual")
    sent = features.get("news_sentiment")
    political = features.get("political_risk")
    market = features.get("market_sentiment")

    rsi_bucket = _bucket(rsi, [30, 45, 55, 70], ["RSI_OS", "RSI_LOW", "RSI_MID", "RSI_HIGH", "RSI_OB"])
    if rsi_bucket:
        patterns.append(f"{rsi_bucket}_{action}")

    if macd is not None and macd_sig is not None:
        side = "MACD_BULL" if macd > macd_sig else "MACD_BEAR"
        patterns.append(f"{side}_{action}")

    vol_bucket = _bucket(vol, [20, 35, 55], ["VOL_LOW", "VOL_MID", "VOL_HIGH", "VOL_EXTREME"])
    if vol_bucket:
        patterns.append(f"{vol_bucket}_{action}")

    sent_bucket = _bucket(sent, [-0.2, 0.2], ["NEWS_NEG", "NEWS_NEUTRAL", "NEWS_POS"])
    if sent_bucket:
        patterns.append(f"{sent_bucket}_{action}")

    if political is not None and political > 0.4:
        patterns.append(f"POLITICAL_RISK_{action}")

    if market:
        patterns.append(f"MARKET_{market}_{action}")

    return patterns


@dataclass
class LearnerVerdict:
    confidence_modifier: float        # умножитель уверенности 0..1.5
    risk_modifier: float              # прибавка к risk_score
    matched: List[Dict[str, Any]]


class Learner:
    def __init__(self, db: AgentDB):
        self.db = db

    # ── Зафиксировать урок ────────────────────────────────────────────────────
    def absorb_closed_trade(self, trade: Dict[str, Any]) -> None:
        decision = self._decision_for(trade.get("decision_id"))
        if decision is None:
            return
        try:
            features = json.loads(decision.get("factors_json") or "{}")
        except Exception:
            features = {}

        action = decision.get("action", "BUY")
        patterns = features_to_patterns(features, action)
        pnl_pct = float(trade.get("pnl_pct") or 0.0)
        is_win = pnl_pct > 0

        for pattern in patterns:
            if is_win:
                title = f"Паттерн {pattern} — успех"
                desc = (
                    f"Сделка по {trade['ticker']} закрылась +{pnl_pct:.2f}% "
                    f"({trade.get('close_reason')}). Повышаем доверие."
                )
                delta = +0.15
            else:
                title = f"Паттерн {pattern} — провал"
                desc = (
                    f"Сделка по {trade['ticker']} закрылась {pnl_pct:.2f}% "
                    f"({trade.get('close_reason')}). Снижаем доверие."
                )
                delta = -0.2
            self.db.upsert_lesson(
                pattern=pattern,
                title=title,
                description=desc,
                trade_id=int(trade["id"]),
                weight_delta=delta,
            )

    def _decision_for(self, decision_id: Optional[int]) -> Optional[Dict[str, Any]]:
        if not decision_id:
            return None
        for d in self.db.recent_decisions(500):
            if d.get("id") == decision_id:
                return d
        return None

    # ── Применить уроки к новому решению ────────────────────────────────────
    def advise(self, features: Dict[str, Any], action: str) -> LearnerVerdict:
        patterns = features_to_patterns(features, action)
        matched = self.db.lessons_for(patterns)
        if not matched:
            return LearnerVerdict(1.0, 0.0, [])

        # weight >1.0 → паттерн «полезный», <1.0 → опасный
        weights = [float(m["weight"]) for m in matched]
        avg = sum(weights) / len(weights)
        # 1.0 — нейтрально; нормируем к диапазону 0.5..1.4
        confidence_modifier = max(0.5, min(1.4, avg))
        # риск растёт, если в среднем уроки негативные
        if avg < 1.0:
            risk_modifier = round((1.0 - avg) * 0.4, 3)
        else:
            risk_modifier = -round((avg - 1.0) * 0.1, 3)

        return LearnerVerdict(
            confidence_modifier=round(confidence_modifier, 3),
            risk_modifier=risk_modifier,
            matched=matched,
        )
