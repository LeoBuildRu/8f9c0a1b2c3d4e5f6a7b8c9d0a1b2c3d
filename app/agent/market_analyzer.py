"""Анализ технического состояния рынка и отдельных тикеров.

Использует уже существующий `fetch_stock_data_with_fallback` и `PricePredictor`,
не дублируя их логику.
"""

from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field, asdict
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

import numpy as np
import pandas as pd
import pandas_ta as ta

from app.data_services import fetch_stock_data_with_fallback

log = logging.getLogger(__name__)

DAILY_INTERVAL = 8           # код дневных свечей (см. app.config.INTERVALS)
TRADING_DAYS_PER_YEAR = 252
SESSION_MINUTES_PER_DAY = 840  # ~14 часов: основная + вечерняя сессия MOEX


def bars_per_year(interval: int) -> float:
    """Сколько свечей в году — для перевода волатильности в годовую."""
    if interval in (DAILY_INTERVAL, 9, 10, 11):
        return TRADING_DAYS_PER_YEAR
    return TRADING_DAYS_PER_YEAR * SESSION_MINUTES_PER_DAY / interval


@dataclass
class TickerSignal:
    ticker: str
    price: float
    rsi: Optional[float]
    macd: Optional[float]
    macd_signal: Optional[float]
    sma20: Optional[float]
    sma50: Optional[float]
    atr_pct: Optional[float]
    volatility_annual: float
    trend_5d: float
    trend_20d: float
    support: Optional[float]
    resistance: Optional[float]
    score: float = 0.0
    direction: str = "NEUTRAL"  # 'BUY' / 'SELL' / 'NEUTRAL'
    reasons: List[str] = field(default_factory=list)
    ml_prediction: Optional[float] = None
    ml_accuracy: Optional[float] = None
    ml_change_pct: Optional[float] = None

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


class MarketAnalyzer:
    """Сводный технический анализ по тикеру и по корзине тикеров."""

    def __init__(
        self,
        stocks_map: Dict[str, int],
        predictor=None,
        lookback_days: int = 120,
        interval: int = DAILY_INTERVAL,
    ):
        """interval — таймфрейм свечей: 8 — дневные, иначе минуты (60 — часовые)."""
        self.stocks_map = stocks_map
        self.lookback_days = lookback_days
        self.predictor = predictor
        self.interval = interval

    # ── Загрузка ──────────────────────────────────────────────────────────────
    def load_candles(self, ticker: str) -> Optional[pd.DataFrame]:
        if ticker not in self.stocks_map:
            return None
        end = datetime.now()
        start = end - timedelta(days=self.lookback_days)
        try:
            df = fetch_stock_data_with_fallback(
                stock_code=ticker,
                stock_id=self.stocks_map[ticker],
                start_date=start,
                end_date=end,
                interval=self.interval,
                timeout=15,
            )
            return df if df is not None and not df.empty else None
        except Exception as exc:
            print(f"[MarketAnalyzer] {ticker}: {exc}")
            return None

    # ── Индикаторы ────────────────────────────────────────────────────────────
    def _add_indicators(self, df: pd.DataFrame) -> pd.DataFrame:
        df = df.copy()
        if len(df) >= 14:
            df["RSI"] = ta.rsi(df["CLOSE"], length=14)
            df["ATR"] = ta.atr(df["HIGH"], df["LOW"], df["CLOSE"], length=14)
        if len(df) >= 26:
            macd = ta.macd(df["CLOSE"])
            if macd is not None and len(macd) == len(df):
                df = pd.concat([df, macd], axis=1)
        if len(df) >= 20:
            df["SMA20"] = ta.sma(df["CLOSE"], length=20)
        if len(df) >= 50:
            df["SMA50"] = ta.sma(df["CLOSE"], length=50)
        return df

    def analyze_ticker(self, ticker: str, df: Optional[pd.DataFrame] = None) -> Optional[TickerSignal]:
        if df is None:
            df = self.load_candles(ticker)
        if df is None or df.empty or len(df) < 20:
            return None

        df = self._add_indicators(df)
        last = df.iloc[-1]
        price = float(last["CLOSE"])

        def _f(name: str) -> Optional[float]:
            if name in df.columns and not pd.isna(last[name]):
                return float(last[name])
            return None

        rsi = _f("RSI")
        macd = _f("MACD_12_26_9")
        macd_sig = _f("MACDs_12_26_9")
        sma20 = _f("SMA20")
        sma50 = _f("SMA50")
        atr = _f("ATR")
        atr_pct = (atr / price * 100.0) if (atr and price) else None

        returns = df["CLOSE"].pct_change().dropna()
        vol_annual = float(returns.std() * np.sqrt(bars_per_year(self.interval)) * 100.0) if not returns.empty else 0.0

        trend_5d = self._trend(df, 5)
        trend_20d = self._trend(df, 20)
        support, resistance = self._levels(df)

        ml_pred = ml_acc = ml_change = None
        if self.predictor is not None and len(df) >= 30:
            try:
                pred, acc, _ = self.predictor.predict_next_day(df, price)
                if pred is not None:
                    ml_pred = float(pred)
                    ml_acc = float(acc) if acc is not None else None
                    ml_change = (ml_pred - price) / price * 100.0
            except Exception as exc:
                print(f"[MarketAnalyzer] ML {ticker}: {exc}")

        sig = TickerSignal(
            ticker=ticker,
            price=price,
            rsi=rsi,
            macd=macd,
            macd_signal=macd_sig,
            sma20=sma20,
            sma50=sma50,
            atr_pct=atr_pct,
            volatility_annual=vol_annual,
            trend_5d=trend_5d,
            trend_20d=trend_20d,
            support=support,
            resistance=resistance,
            ml_prediction=ml_pred,
            ml_accuracy=ml_acc,
            ml_change_pct=ml_change,
        )
        self._score(sig)
        return sig

    # ── Скоринг сигнала ──────────────────────────────────────────────────────
    def _score(self, s: TickerSignal) -> None:
        score = 0.0
        reasons: List[str] = []

        if s.rsi is not None:
            if s.rsi < 30:
                score += 2.0
                reasons.append(f"RSI={s.rsi:.1f} (перепроданность)")
            elif s.rsi < 40:
                score += 1.0
                reasons.append(f"RSI={s.rsi:.1f} (умеренная перепроданность)")
            elif s.rsi > 70:
                score -= 2.0
                reasons.append(f"RSI={s.rsi:.1f} (перекупленность)")
            elif s.rsi > 60:
                score -= 1.0
                reasons.append(f"RSI={s.rsi:.1f} (умеренная перекупленность)")

        if s.macd is not None and s.macd_signal is not None:
            if s.macd > s.macd_signal and s.macd > 0:
                score += 1.5
                reasons.append("MACD: бычье пересечение выше нуля")
            elif s.macd > s.macd_signal:
                score += 0.7
                reasons.append("MACD: бычье пересечение")
            elif s.macd < s.macd_signal and s.macd < 0:
                score -= 1.5
                reasons.append("MACD: медвежье пересечение ниже нуля")
            else:
                score -= 0.7
                reasons.append("MACD: медвежье пересечение")

        if s.sma20 and s.sma50:
            if s.price > s.sma20 > s.sma50:
                score += 1.2
                reasons.append("Цена выше SMA20 > SMA50 (восходящий тренд)")
            elif s.price < s.sma20 < s.sma50:
                score -= 1.2
                reasons.append("Цена ниже SMA20 < SMA50 (нисходящий тренд)")

        if s.trend_20d > 4:
            score += 0.5
            reasons.append(f"20-дневный тренд +{s.trend_20d:.1f}%")
        elif s.trend_20d < -4:
            score -= 0.5
            reasons.append(f"20-дневный тренд {s.trend_20d:.1f}%")

        if s.ml_change_pct is not None and s.ml_accuracy is not None and s.ml_accuracy > 55:
            weight = min(2.0, s.ml_accuracy / 50.0)
            if s.ml_change_pct > 1.0:
                score += weight
                reasons.append(f"ML: +{s.ml_change_pct:.2f}% при точности {s.ml_accuracy:.0f}%")
            elif s.ml_change_pct < -1.0:
                score -= weight
                reasons.append(f"ML: {s.ml_change_pct:.2f}% при точности {s.ml_accuracy:.0f}%")

        s.score = round(score, 2)
        s.reasons = reasons
        if score >= 2.0:
            s.direction = "BUY"
        elif score <= -2.0:
            s.direction = "SELL"
        else:
            s.direction = "NEUTRAL"

    # ── Утилиты ──────────────────────────────────────────────────────────────
    @staticmethod
    def _trend(df: pd.DataFrame, period: int) -> float:
        if len(df) < period + 1:
            return 0.0
        a = df["CLOSE"].iloc[-period]
        b = df["CLOSE"].iloc[-1]
        return float((b - a) / a * 100.0) if a else 0.0

    @staticmethod
    def _levels(df: pd.DataFrame) -> tuple[Optional[float], Optional[float]]:
        if len(df) < 20:
            return None, None
        window = df.tail(20)
        return float(window["LOW"].min()), float(window["HIGH"].max())

    # ── Сканирование вселенной ──────────────────────────────────────────────
    def scan_universe(
        self,
        tickers: Optional[List[str]] = None,
        max_workers: int = 12,
    ) -> List[TickerSignal]:
        """Параллельно сканирует тикеры (I/O-bound: загрузка свечей MOEX).

        На 100 тикерах: ~15–25 секунд против ~3–5 минут последовательно.
        """
        tickers = tickers or list(self.stocks_map.keys())
        signals: List[TickerSignal] = []
        if not tickers:
            return signals

        if len(tickers) <= 1 or max_workers <= 1:
            for t in tickers:
                sig = self.analyze_ticker(t)
                if sig is not None:
                    signals.append(sig)
        else:
            workers = min(max_workers, len(tickers))
            with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="scan") as pool:
                future_map = {pool.submit(self._safe_analyze, t): t for t in tickers}
                for future in as_completed(future_map):
                    sig = future.result()
                    if sig is not None:
                        signals.append(sig)

        signals.sort(key=lambda x: x.score, reverse=True)
        return signals

    def _safe_analyze(self, ticker: str) -> Optional[TickerSignal]:
        try:
            return self.analyze_ticker(ticker)
        except Exception as exc:
            log.warning("scan %s: %s", ticker, exc)
            return None

    @staticmethod
    def market_sentiment(signals: List[TickerSignal]) -> Dict[str, Any]:
        if not signals:
            return {"label": "UNKNOWN", "ratio_up": 0.0, "avg_score": 0.0}
        positive = sum(1 for s in signals if s.score > 0)
        ratio = positive / len(signals)
        avg = float(np.mean([s.score for s in signals]))
        if ratio > 0.6 and avg > 0.5:
            label = "BULL"
        elif ratio < 0.4 and avg < -0.5:
            label = "BEAR"
        else:
            label = "NEUTRAL"
        return {"label": label, "ratio_up": ratio, "avg_score": avg}
