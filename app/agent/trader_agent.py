"""Главный торговый агент.

Цикл работы (вызывается из UI таймером или вручную):
1. Сканирует тикеры пула по часовым свечам, собирает TickerSignal.
2. Оценивает новостной фон (общий + per-ticker).
3. Сверяет с уроками из БД.
4. Принимает решение: открыть лонг или шорт, держать, закрыть.
5. Для каждой новой сделки сам определяет горизонт (не дольше суток),
   стоп-лосс и тейк-профит — на основе ATR и силы сигнала.
6. На каждом тике проверяет открытые сделки на SL/TP/таймаут/смену сигнала
   и закрывает их.
7. Каждый закрытый трейд скармливается Learner.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, time, timedelta
from typing import Any, Callable, Dict, List, Optional, Tuple

from app.data_services import fetch_stock_data_with_fallback
from app.agent.database import AgentDB
from app.agent.market_analyzer import MarketAnalyzer, TickerSignal
from app.agent.news_analyzer import NewsAnalyzer, NewsAssessment
from app.agent.portfolio import Portfolio
from app.agent.risk import RiskManager, RiskParams, TradePlan
from app.agent.learner import Learner

AGENT_CAPITAL_LIMIT = 1_000_000.0

# Топ-25 ликвидных эмитентов — широкий, но всё ещё качественный пул.
# На нём же проверяется стратегия в backtest.py.
PRIORITY_TICKERS = [
    "SBER", "GAZP", "LKOH", "ROSN", "NVTK", "GMKN", "PLZL", "TATN",
    "MGNT", "T", "MOEX", "ALRS", "CHMF", "MTSS", "YDEX",
    "RUAL", "AFKS", "PHOR", "OZON", "VTBR", "IRAO", "AFLT",
    "SNGS", "HYDR", "RTKM",
]

# Основная сессия MOEX по Москве (UTC+3, без перехода на летнее время)
MSK_OFFSET = timedelta(hours=3)
SESSION_OPEN = time(10, 0)
SESSION_CLOSE = time(18, 40)

# Короткие сделки: часовые свечи за последний месяц, горизонт не больше суток.
CANDLE_INTERVAL = 60
LOOKBACK_DAYS = 30
MAX_HORIZON_HOURS = 24.0
# Часовой ATR — доли процента, поэтому стопы уже, чем у дневной стратегии.
INTRADAY_RISK = RiskParams(min_stop_pct=0.5, max_stop_pct=4.0)


@dataclass
class AgentTickReport:
    started_at: str
    finished_at: str
    sentiment: Dict[str, Any]
    news: Dict[str, Any]
    decisions: List[Dict[str, Any]] = field(default_factory=list)
    opened: List[Dict[str, Any]] = field(default_factory=list)
    closed: List[Dict[str, Any]] = field(default_factory=list)
    skipped: List[Dict[str, Any]] = field(default_factory=list)
    error: Optional[str] = None

    def summary(self) -> str:
        return (
            f"Тик {self.started_at} → {self.finished_at}: "
            f"открыто {len(self.opened)}, закрыто {len(self.closed)}, "
            f"пропущено {len(self.skipped)}. Рынок: {self.sentiment.get('label')}, "
            f"новости {self.news.get('overall_sentiment')}."
        )


class TraderAgent:
    """Синхронный API. Не зависит от Qt — работает и в desktop UI, и на сервере.

    Можно передать колбэки:
    - on_trade_opened(dict)  — вызывается после открытия каждой сделки
    - on_trade_closed(dict)  — вызывается после закрытия каждой сделки
    - on_tick_done(report)   — вызывается в конце каждого tick()
    """

    def __init__(
        self,
        stocks_map: Dict[str, int],
        predictor=None,
        capital_limit: float = AGENT_CAPITAL_LIMIT,
        db_path: Optional[str] = None,
        risk_params: Optional[RiskParams] = None,
        on_trade_opened: Optional[Callable[[Dict[str, Any]], None]] = None,
        on_trade_closed: Optional[Callable[[Dict[str, Any]], None]] = None,
        on_tick_done: Optional[Callable[["AgentTickReport"], None]] = None,
        news_analyzer: Optional[NewsAnalyzer] = None,
        regime_sma: Optional[int] = None,
        interval: int = CANDLE_INTERVAL,
        lookback_days: int = LOOKBACK_DAYS,
    ):
        self.stocks_map = stocks_map
        self.db = AgentDB(db_path, initial_capital=capital_limit) if db_path else AgentDB(initial_capital=capital_limit)
        self.portfolio = Portfolio(self.db)
        self.risk = RiskManager(self.portfolio, risk_params or INTRADAY_RISK)
        self.market = MarketAnalyzer(stocks_map, predictor=predictor, lookback_days=lookback_days, interval=interval)
        self.news = news_analyzer or NewsAnalyzer()
        self.learner = Learner(self.db)
        # Порог входа: 1.5 даёт сделки на спокойном рынке,
        # сохраняя качество сетапа (нужно как минимум одно сильное условие
        # — например, бычий MACD выше нуля + поддержка тренда).
        # Для шорта порог зеркальный: score <= -1.5.
        self.min_signal_score = 1.5
        self.min_confidence = 0.22
        self.commission_pct = self.risk.p.commission_pct
        # Фильтр рынка по IMOEX: лонги — только когда индекс выше своей
        # SMA(regime_sma), шорты — только когда ниже. По умолчанию выключен:
        # агент торгует в обе стороны при любом рынке.
        self.regime_sma = regime_sma

        self.on_trade_opened = on_trade_opened
        self.on_trade_closed = on_trade_closed
        self.on_tick_done = on_tick_done

    def _safe_call(self, cb: Optional[Callable], *args) -> None:
        if cb is None:
            return
        try:
            cb(*args)
        except Exception as exc:
            print(f"[TraderAgent] callback error: {exc}")

    # ── Главный шаг ──────────────────────────────────────────────────────────
    def tick(self, scan_tickers: Optional[List[str]] = None) -> AgentTickReport:
        report = AgentTickReport(
            started_at=datetime.utcnow().isoformat(timespec="seconds"),
            finished_at="",
            sentiment={},
            news={},
        )
        try:
            tickers = scan_tickers or self._candidate_tickers()
            signals = self.market.scan_universe(tickers)
            sentiment = self.market.market_sentiment(signals)
            news = self.news.assess(tickers=[s.ticker for s in signals])

            report.sentiment = sentiment
            report.news = news.to_dict()

            # 1. Сначала менеджим открытые позиции
            closed = self._manage_open_positions(signals, news, sentiment)
            report.closed.extend(closed)

            # 2. Затем смотрим, можем ли вообще что-то открывать
            ok, reason = self.risk.can_open_new()
            if ok and not self._session_open():
                ok, reason = False, "основная сессия MOEX закрыта"
            allowed_sides = {"LONG", "SHORT"}
            if ok and self.regime_sma:
                up, note = self._market_regime()
                report.sentiment["regime"] = note
                allowed_sides = set() if up is None else {"LONG"} if up else {"SHORT"}
                if not allowed_sides:
                    ok, reason = False, note
            if not ok:
                report.skipped.append({"reason": reason})
                self.db.log_decision(
                    ticker=None,
                    action="HOLD",
                    confidence=0.0,
                    risk_score=0.5,
                    rationale=f"Не открываем новые: {reason}.",
                    factors={"sentiment": sentiment, "news": news.to_dict()},
                )
            else:
                # 3. Кандидаты в обе стороны; первыми — те, что быстрее дойдут до цели
                already_open = {p["ticker"] for p in self.portfolio.open_positions()}
                candidates = [
                    s for s in signals
                    if s.ticker not in already_open and self._side_for(s) in allowed_sides
                ]
                candidates.sort(key=self._speed_priority, reverse=True)

                # Если совсем нечего открывать — логируем топ-3 «почти прошедших»,
                # чтобы было видно, что агент смотрел и думал.
                if not candidates:
                    near_miss = sorted(
                        (s for s in signals if s.ticker not in already_open and s.score != 0),
                        key=lambda s: abs(s.score),
                        reverse=True,
                    )[:3]
                    for s in near_miss:
                        self.db.log_decision(
                            ticker=s.ticker,
                            action="SKIP",
                            confidence=round(min(1.0, abs(s.score) / 6.0), 2),
                            risk_score=0.0,
                            rationale=(
                                f"Score {s.score:+.2f}, порог ±{self.min_signal_score}. "
                                + (("Причины: " + "; ".join(s.reasons[:3])) if s.reasons else "")
                            ),
                            factors=self._features_for(s, news, sentiment),
                        )
                        report.skipped.append({
                            "ticker": s.ticker,
                            "reason": f"score {s.score:+.2f}, порог ±{self.min_signal_score}",
                        })

                for sig in candidates[:3]:
                    result = self._try_open(sig, news, sentiment)
                    if result.get("opened"):
                        report.opened.append(result)
                    else:
                        report.skipped.append(result)

            # 4. Общий HOLD-маркер, если ничего не делали
            if not report.opened and not report.closed and not report.skipped:
                self.db.log_decision(
                    ticker=None,
                    action="HOLD",
                    confidence=0.0,
                    risk_score=0.0,
                    rationale=f"Нет подходящих сетапов. Рынок: {sentiment.get('label')}.",
                    factors={"sentiment": sentiment, "news": news.to_dict()},
                )
        except Exception as exc:
            report.error = f"{type(exc).__name__}: {exc}"
        finally:
            report.finished_at = datetime.utcnow().isoformat(timespec="seconds")
        self._safe_call(self.on_tick_done, report)
        return report

    # ── Фильтры входа ────────────────────────────────────────────────────────
    @staticmethod
    def _session_open(now_utc: Optional[datetime] = None) -> bool:
        """Идёт ли основная сессия: вне её цена — вчерашняя, а не рыночная.
        Праздники не учитываются: в такие дни просто не будет свежих свечей."""
        msk = (now_utc or datetime.utcnow()) + MSK_OFFSET
        return msk.weekday() < 5 and SESSION_OPEN <= msk.time() <= SESSION_CLOSE

    def _market_regime(self) -> Tuple[Optional[bool], str]:
        """IMOEX выше своей SMA(regime_sma)? None — нет данных для решения."""
        end = datetime.now()
        try:
            df = fetch_stock_data_with_fallback(
                "IMOEX", self.stocks_map.get("IMOEX", 0),
                end - timedelta(days=self.regime_sma * 2), end, interval=8,
            )
        except Exception as exc:
            return None, f"нет данных IMOEX для фильтра рынка ({exc})"
        if df is None or len(df) < self.regime_sma:
            return None, "мало данных IMOEX для фильтра рынка"
        close = df["CLOSE"]
        last, sma = float(close.iloc[-1]), float(close.rolling(self.regime_sma).mean().iloc[-1])
        if last > sma:
            return True, f"IMOEX {last:.0f} выше SMA{self.regime_sma} {sma:.0f} — только лонги"
        return False, f"IMOEX {last:.0f} ниже SMA{self.regime_sma} {sma:.0f} — только шорты"

    # ── Кандидаты ────────────────────────────────────────────────────────────
    def _candidate_tickers(self) -> List[str]:
        return [t for t in PRIORITY_TICKERS if t in self.stocks_map]

    def _side_for(self, sig: TickerSignal) -> Optional[str]:
        if sig.direction == "BUY" and sig.score >= self.min_signal_score:
            return "LONG"
        if sig.direction == "SELL" and sig.score <= -self.min_signal_score:
            return "SHORT"
        return None

    @staticmethod
    def _speed_priority(sig: TickerSignal) -> float:
        """Сила сигнала × часовая волатильность: чем больше, тем быстрее цена
        проходит расстояние до тейка — такие сделки берём первыми."""
        return abs(sig.score) * (sig.atr_pct or 0.0)

    # ── Открытие сделки ──────────────────────────────────────────────────────
    def _try_open(
        self,
        sig: TickerSignal,
        news: NewsAssessment,
        sentiment: Dict[str, Any],
    ) -> Dict[str, Any]:
        side = self._side_for(sig)
        action = "BUY" if side == "LONG" else "SHORT"
        news_for_ticker = news.per_ticker.get(sig.ticker, 0.0)
        # Не идём против резкого новостного фона: в лонг при плохих новостях,
        # в шорт — при хороших.
        news_against = -news_for_ticker if side == "LONG" else news_for_ticker
        if news_against >= 0.4:
            self.db.log_decision(
                ticker=sig.ticker,
                action="SKIP",
                confidence=0.0,
                risk_score=0.8,
                rationale=f"Новостной фон ({news_for_ticker:+.2f}) против {side} по {sig.ticker}",
                factors={"signal": sig.to_dict(), "news_for_ticker": news_for_ticker},
            )
            return {"opened": False, "ticker": sig.ticker, "reason": "news against"}

        # Базовая уверенность по силе сигнала
        confidence = max(0.0, min(1.0, abs(sig.score) / 6.0))
        # Поднимаем уверенность, если ML согласен с направлением
        ml_agrees = sig.ml_change_pct is not None and (sig.ml_change_pct > 0) == (side == "LONG")
        if ml_agrees and sig.ml_accuracy and sig.ml_accuracy > 60:
            confidence = min(1.0, confidence + 0.15)

        features = self._features_for(sig, news, sentiment)
        verdict = self.learner.advise(features, action)
        confidence = max(0.0, min(1.0, confidence * verdict.confidence_modifier))

        risk_score = self.risk.overall_risk_score(
            signal_score=sig.score,
            political_risk=news.political_risk,
            volatility=sig.volatility_annual,
        )
        risk_score = min(1.0, max(0.0, risk_score + verdict.risk_modifier))

        if confidence < self.min_confidence:
            self.db.log_decision(
                ticker=sig.ticker,
                action="SKIP",
                confidence=confidence,
                risk_score=risk_score,
                rationale=(
                    f"Низкая уверенность {confidence:.2f} (< {self.min_confidence}) "
                    f"после применения уроков"
                ),
                factors=features,
            )
            return {"opened": False, "ticker": sig.ticker, "reason": "low confidence"}

        if risk_score > 0.85:
            self.db.log_decision(
                ticker=sig.ticker,
                action="SKIP",
                confidence=confidence,
                risk_score=risk_score,
                rationale=f"Слишком высокий риск {risk_score:.2f}",
                factors=features,
            )
            return {"opened": False, "ticker": sig.ticker, "reason": "risk too high"}

        plan = self.risk.plan_trade(
            ticker=sig.ticker,
            price=sig.price,
            atr_pct=sig.atr_pct,
            political_risk=news.political_risk,
            confidence=confidence,
            side=side,
        )
        if plan is None or plan.qty <= 0:
            self.db.log_decision(
                ticker=sig.ticker,
                action="SKIP",
                confidence=confidence,
                risk_score=risk_score,
                rationale=(
                    "Риск-менеджер не разрешил вход: мало кэша или лимит экспозиции — "
                    "позиция вышла бы меньше трети обычной"
                ),
                factors=features,
            )
            return {"opened": False, "ticker": sig.ticker, "reason": "no qty"}

        # Горизонт сделки агент выбирает сам
        horizon_hours = self._choose_horizon(sig, confidence)
        deadline_at = (datetime.utcnow() + timedelta(hours=horizon_hours)).isoformat(timespec="seconds")

        rationale = self._build_rationale(sig, plan, news, sentiment, horizon_hours, verdict)
        decision_id = self.db.log_decision(
            ticker=sig.ticker,
            action=action,
            confidence=confidence,
            risk_score=risk_score,
            rationale=rationale,
            factors=features,
        )
        self.db.save_snapshot(decision_id, sig.ticker, {
            **features,
            "plan": plan.__dict__,
            "horizon_hours": horizon_hours,
        })

        self.portfolio.reserve_for_buy(plan.qty, plan.entry_price, self.commission_pct)
        trade_id = self.db.open_trade(
            ticker=sig.ticker,
            side=side,
            qty=plan.qty,
            entry_price=plan.entry_price,
            stop_loss=plan.stop_loss,
            take_profit=plan.take_profit,
            horizon_hours=horizon_hours,
            deadline_at=deadline_at,
            decision_id=decision_id,
        )

        result = {
            "opened": True,
            "trade_id": trade_id,
            "decision_id": decision_id,
            "ticker": sig.ticker,
            "side": side,
            "qty": plan.qty,
            "entry_price": plan.entry_price,
            "stop_loss": plan.stop_loss,
            "take_profit": plan.take_profit,
            "horizon_hours": horizon_hours,
            "deadline_at": deadline_at,
            "confidence": confidence,
            "risk_score": risk_score,
            "rationale": rationale,
        }
        self._safe_call(self.on_trade_opened, result)
        return result

    def _choose_horizon(self, sig: TickerSignal, confidence: float) -> float:
        """Сам решает, в течение скольких часов хочет закрыть сделку (не дольше суток)."""
        base = 12.0
        # Чем выше волатильность, тем короче горизонт
        if sig.volatility_annual > 50:
            base *= 0.6
        elif sig.volatility_annual < 20:
            base *= 1.4
        # Чем выше уверенность, тем дольше готовы держать
        base *= 0.6 + confidence
        # Скоринг направления тоже влияет
        if abs(sig.score) >= 4:
            base *= 1.2
        return float(round(max(2.0, min(base, MAX_HORIZON_HOURS)), 1))

    # ── Менеджмент открытых ──────────────────────────────────────────────────
    def _manage_open_positions(
        self,
        signals: List[TickerSignal],
        news: NewsAssessment,
        sentiment: Dict[str, Any],
    ) -> List[Dict[str, Any]]:
        closed: List[Dict[str, Any]] = []
        signals_by_ticker = {s.ticker: s for s in signals}
        now = datetime.utcnow()

        for pos in list(self.portfolio.open_positions()):
            ticker = pos["ticker"]
            sig = signals_by_ticker.get(ticker) or self.market.analyze_ticker(ticker)
            if sig is None:
                continue
            price = sig.price
            is_long = pos.get("side", "LONG") == "LONG"
            ticker_news = news.per_ticker.get(ticker, 0.0)

            reason: Optional[str] = None
            if (price <= pos["stop_loss"]) if is_long else (price >= pos["stop_loss"]):
                reason = "SL"
            elif (price >= pos["take_profit"]) if is_long else (price <= pos["take_profit"]):
                reason = "TP"
            else:
                deadline = self._parse_iso(pos["deadline_at"])
                if deadline and now >= deadline:
                    reason = "TIMEOUT"
                # Жёсткая смена сигнала против позиции
                elif (sig.direction == "SELL" and sig.score <= -2.5) if is_long else (sig.direction == "BUY" and sig.score >= 2.5):
                    reason = "SIGNAL_FLIP"
                # Резкие новости против позиции
                elif (ticker_news <= -0.6) if is_long else (ticker_news >= 0.6):
                    reason = "NEWS_FLIP"

            if reason:
                self.portfolio.release_on_close(pos, price, self.commission_pct)
                closed_trade = self.db.close_trade(
                    pos["id"], close_price=price, reason=reason, commission_pct=self.commission_pct,
                )
                self.db.log_decision(
                    ticker=ticker,
                    action="CLOSE",
                    confidence=1.0,
                    risk_score=0.0,
                    rationale=f"Закрытие {pos.get('side', 'LONG')} по {reason}. P&L = {closed_trade.get('pnl_abs', 0):+.2f} ₽ ({closed_trade.get('pnl_pct', 0):+.2f}%).",
                    factors={"signal": sig.to_dict(), "reason": reason},
                )
                self.learner.absorb_closed_trade(closed_trade)
                self._safe_call(self.on_trade_closed, closed_trade)
                closed.append(closed_trade)
        return closed

    # ── Утилиты ──────────────────────────────────────────────────────────────
    @staticmethod
    def _parse_iso(value: Any) -> Optional[datetime]:
        if not value:
            return None
        try:
            return datetime.fromisoformat(str(value))
        except ValueError:
            return None

    def _features_for(
        self,
        sig: TickerSignal,
        news: NewsAssessment,
        sentiment: Dict[str, Any],
    ) -> Dict[str, Any]:
        return {
            "ticker": sig.ticker,
            "price": sig.price,
            "rsi": sig.rsi,
            "macd": sig.macd,
            "macd_signal": sig.macd_signal,
            "sma20": sig.sma20,
            "sma50": sig.sma50,
            "atr_pct": sig.atr_pct,
            "volatility_annual": sig.volatility_annual,
            "trend_5d": sig.trend_5d,
            "trend_20d": sig.trend_20d,
            "score": sig.score,
            "ml_prediction": sig.ml_prediction,
            "ml_accuracy": sig.ml_accuracy,
            "ml_change_pct": sig.ml_change_pct,
            "news_sentiment": news.per_ticker.get(sig.ticker, news.overall_sentiment),
            "overall_news_sentiment": news.overall_sentiment,
            "political_risk": news.political_risk,
            "market_sentiment": sentiment.get("label"),
        }

    def _build_rationale(
        self,
        sig: TickerSignal,
        plan: TradePlan,
        news: NewsAssessment,
        sentiment: Dict[str, Any],
        horizon_hours: float,
        verdict,
    ) -> str:
        verb = "Покупка" if plan.side == "LONG" else "Шорт"
        parts = [
            f"{verb} {plan.qty} {sig.ticker} @ {plan.entry_price:.2f} ₽.",
            f"Сигнал-скор {sig.score:+.2f} ({sig.direction}). Причины: "
            + "; ".join(sig.reasons[:4]) + ".",
            f"SL {plan.stop_loss:.2f} / TP {plan.take_profit:.2f} (R:R = {plan.risk_reward}, риск {plan.risk_amount:.0f} ₽).",
            f"Горизонт ≤ {horizon_hours:.0f} ч.",
            f"Рынок: {sentiment.get('label')} (доля растущих {sentiment.get('ratio_up', 0)*100:.0f}%).",
            f"Новости общий {news.overall_sentiment:+.2f}, по тикеру {news.per_ticker.get(sig.ticker, 0):+.2f}, политический риск {news.political_risk:.2f}.",
        ]
        if verdict.matched:
            best = max(verdict.matched, key=lambda m: m["weight"])
            worst = min(verdict.matched, key=lambda m: m["weight"])
            parts.append(
                f"Уроки: лучший паттерн {best['pattern']} (w={best['weight']:.2f}), "
                f"худший {worst['pattern']} (w={worst['weight']:.2f}). "
                f"Модификатор уверенности ×{verdict.confidence_modifier}."
            )
        return " ".join(parts)


# UI-обёртку (QThread) см. в app.agent.qt_worker
