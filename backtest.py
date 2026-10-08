"""Бэктест правил торгового агента на истории MOEX.

Зачем: проверить стратегию на нескольких годах истории, а не ждать месяцами
живого paper-трейдинга. Код агента не меняется — переиспользуются
MarketAnalyzer (скоринг), RiskManager (размер позиции, SL/TP) и правила
TraderAgent (выбор стороны, приоритет, горизонт сделки).

Две версии агента:
- agent    — прежняя дневная: дневные свечи, только лонг, горизонт до 10 дней;
- agent_h1 — текущая: часовые свечи, лонг и шорт, сделки до 24 часов,
  вход только в основную сессию (10:00–18:40 МСК, будни).

Допущения (скорее консервативные):
- сигнал считается по закрытию свечи, вход — по открытию следующей;
- если за свечу задеты и SL, и TP — считаем, что первым сработал SL;
- гэп через SL/TP исполняется по цене открытия;
- комиссия 0.05% на сторону; плата за перенос шорта через ночь не учтена;
- дивиденды не начисляются (как и в агенте), а дивидендный гэп выглядит
  как обычное падение цены: MOEX больше не отдаёт дивиденды в открытом API;
- новостей в истории нет: новостной фон нейтральный; ML по умолчанию
  выключен (--ml включает, но считается долго).

Запуск (тяжёлые прогоны — на сервере, а не на рабочем компьютере):
    python backtest.py                         # дневной агент + случайный вход для сравнения
    python backtest.py --strategy agent_h1     # текущий агент на часовых свечах
    python backtest.py --strategy agent_h1 --tickers SBER,NLMK,MAGN   # свой список бумаг
    python backtest.py --strategy agent_h1 --experiments   # гипотезы по часовой стратегии
    python backtest.py --sweep                 # сетка множителей SL/TP: winrate против доходности
    python backtest.py --strategy momentum     # альтернатива: ротация по моментуму
    python backtest.py --research --period is  # все дневные эксперименты на периоде подбора 2023–2024
    python backtest.py --research --period oos # те же эксперименты на проверке 2025–2026
"""

from __future__ import annotations

import argparse
import bisect
import math
import random
import statistics
import time as time_mod
import warnings
from concurrent.futures import ProcessPoolExecutor
from dataclasses import dataclass, field, replace
from datetime import datetime, time, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Dict, List, Optional, Tuple

import pandas as pd
import pandas_ta as ta
import requests

warnings.filterwarnings("ignore")

from app.agent.market_analyzer import MarketAnalyzer
from app.agent.risk import RiskManager, RiskParams
from app.agent.trader_agent import (
    INTRADAY_RISK, LOOKBACK_DAYS as H1_LOOKBACK_DAYS, PRIORITY_TICKERS,
    SESSION_CLOSE, SESSION_OPEN, TraderAgent,
)
from app.data_services import fetch_moex_data

BASE_DIR = Path(__file__).resolve().parent
HISTORY_DIR = BASE_DIR / "data" / "history"
DATA_START = datetime(2022, 1, 1)
H1_DATA_START = datetime(2022, 11, 15)  # месяц прогрева до 2023 года
DAILY, HOURLY = 8, 60
LOOKBACK_DAYS = 120  # как MarketAnalyzer.lookback_days дневной версии

# Параметры подбираем на одном периоде, а проверяем на другом, который при
# подборе не смотрели. Иначе легко «найти» правила, подогнанные под историю.
PERIODS = {
    "is": (datetime(2023, 1, 1), datetime(2024, 12, 31, 23, 59)),
    "oos": (datetime(2025, 1, 1), None),
    "all": (datetime(2023, 1, 1), None),
}

UNIVERSE = PRIORITY_TICKERS


# ── Данные ──────────────────────────────────────────────────────────────────
def _polite_get(url: str, params: Optional[dict] = None, timeout: float = 30) -> requests.Response:
    """MOEX рвёт соединение, если слать ему сотни запросов подряд, — а с того же
    IP ходит живой агент. Поэтому пауза между запросами и повторы с отступом."""
    for attempt in range(5):
        time_mod.sleep(0.5)
        try:
            return requests.get(url, params=params, timeout=timeout)
        except requests.RequestException as exc:
            print(f"  MOEX: {type(exc).__name__}, повтор через {10 * (attempt + 1)} с", flush=True)
            time_mod.sleep(10 * (attempt + 1))
    return requests.get(url, params=params, timeout=timeout)


def load_history(ticker: str, end: datetime, refresh: bool = False,
                 board: Optional[str] = None, interval: int = DAILY) -> pd.DataFrame:
    suffix = "" if interval == DAILY else f"_m{interval}"
    path = HISTORY_DIR / f"{ticker}{suffix}.csv"
    if path.exists() and not refresh:
        return pd.read_csv(path, parse_dates=["DATE"])
    start = DATA_START if interval == DAILY else H1_DATA_START
    df = fetch_moex_data(ticker, start, end, interval=interval, timeout=30, board=board, http_get=_polite_get)
    if not df.empty:
        HISTORY_DIR.mkdir(parents=True, exist_ok=True)
        df.to_csv(path, index=False)
    return df


def load_lqdt(end: datetime, refresh: bool = False) -> pd.Series:
    """Цены фонда LQDT. В 2026 году фонд переехал с режима TQTF на TQBR,
    поэтому история склеивается из обоих; без свежих данных — из кэша."""
    path = HISTORY_DIR / "LQDT.csv"
    if refresh or not path.exists():
        parts = [fetch_moex_data("LQDT", DATA_START, end, interval=DAILY, timeout=30, board=b, http_get=_polite_get)
                 for b in ("TQTF", "TQBR")]
        parts = [p for p in parts if not p.empty]
        if parts:
            HISTORY_DIR.mkdir(parents=True, exist_ok=True)
            pd.concat(parts).drop_duplicates("DATE", keep="last").sort_values("DATE").to_csv(path, index=False)
    if not path.exists():
        return pd.Series(dtype=float)
    return pd.read_csv(path, parse_dates=["DATE"]).set_index("DATE")["CLOSE"]


# ── Сигналы ─────────────────────────────────────────────────────────────────
# Колонки сигнала: priority — порядок отбора кандидатов, score — сила сигнала
# со знаком, buy/sell — вход в лонг или шорт, exit_long/exit_short — выход по
# смене сигнала. Остальные колонки (признаки для ML) симуляция не читает.
SIG_COLS = ["priority", "score", "buy", "sell", "exit_long", "exit_short", "atr_pct", "conf", "horizon_h"]
SIGNALS_DIR = BASE_DIR / "data" / "signals"


def legacy_daily_horizon(sig, confidence: float) -> float:
    """Горизонт дневной версии агента (до перехода на сделки до суток) —
    чтобы дневные эксперименты оставались воспроизводимыми."""
    base = 72.0
    if sig.volatility_annual > 50:
        base *= 0.6
    elif sig.volatility_annual < 20:
        base *= 1.4
    base *= 0.6 + confidence
    if sig.score >= 4:
        base *= 1.2
    return float(round(max(12.0, min(base, 240.0)), 1))


def agent_signals(ticker: str, df: pd.DataFrame, start: datetime, use_ml: bool = False) -> pd.DataFrame:
    """Дневная версия агента — тем же скорингом и на том же окне, что была в проде."""
    predictor = None
    if use_ml:
        from ollama_worker import PricePredictor
        predictor = PricePredictor()
    analyzer = MarketAnalyzer({ticker: 0}, predictor=predictor)
    dates = df["DATE"]
    rows = []
    for i in range(dates.searchsorted(start - timedelta(days=7)), len(df)):
        d = dates.iloc[i]
        lo = dates.searchsorted(d - timedelta(days=LOOKBACK_DAYS), side="right")
        sig = analyzer.analyze_ticker(ticker, df=df.iloc[lo:i + 1].reset_index(drop=True))
        if sig is None:
            continue
        conf = max(0.0, min(1.0, sig.score / 6.0))
        if sig.ml_change_pct is not None and sig.ml_change_pct > 0 and sig.ml_accuracy and sig.ml_accuracy > 60:
            conf = min(1.0, conf + 0.15)
        rows.append({
            "DATE": d,
            "priority": sig.score,
            "score": sig.score,
            # min_signal_score=1.5, но direction=BUY требует score>=2; порог
            # уверенности 0.22 и risk_score<=0.85 без новостей проходят всегда.
            "buy": sig.direction == "BUY" and sig.score >= 1.5,
            "sell": False,
            "exit_long": sig.direction == "SELL" and sig.score <= -2.5,
            "exit_short": False,
            "atr_pct": sig.atr_pct,
            "conf": conf,
            "horizon_h": legacy_daily_horizon(sig, conf),
        })
    return pd.DataFrame(rows)


# Пороги, с которыми TraderAgent выбирает сторону сделки (см. TraderAgent.__init__)
_AGENT_THRESHOLDS = SimpleNamespace(min_signal_score=1.5)


def agent_h1_signals(ticker: str, df: pd.DataFrame, start: datetime, use_ml: bool = False) -> pd.DataFrame:
    """Текущий агент: часовые свечи за 30 дней, сигналы в лонг и в шорт.

    Сигналы считаем только по свечам, после которых агент может войти
    (следующая свеча начинается в основную сессию), — это в разы быстрее,
    а SL/TP/таймаут всё равно проверяются по каждой свече.
    """
    analyzer = MarketAnalyzer({ticker: 0}, lookback_days=H1_LOOKBACK_DAYS, interval=HOURLY)
    dates = df["DATE"]
    rows = []
    for i in range(dates.searchsorted(start - timedelta(days=2)), len(df)):
        d = dates.iloc[i]
        if not TraderAgent.signal_bar_ok(d):
            continue
        lo = dates.searchsorted(d - timedelta(days=H1_LOOKBACK_DAYS), side="right")
        sig = analyzer.analyze_ticker(ticker, df=df.iloc[lo:i + 1].reset_index(drop=True))
        if sig is None:
            continue
        side = TraderAgent._side_for(_AGENT_THRESHOLDS, sig)
        conf = max(0.0, min(1.0, abs(sig.score) / 6.0))
        rows.append({
            "DATE": d,
            "priority": TraderAgent._speed_priority(sig),
            "score": sig.score,
            "buy": side == "LONG",
            "sell": side == "SHORT",
            "exit_long": sig.direction == "SELL" and sig.score <= -2.5,
            "exit_short": sig.direction == "BUY" and sig.score >= 2.5,
            "atr_pct": sig.atr_pct,
            "conf": conf,
            "horizon_h": TraderAgent._choose_horizon(None, sig, conf),
            # Признаки для ML-экспериментов — симуляция их не использует
            "price": sig.price,
            "rsi": sig.rsi,
            "macd_hist": None if sig.macd is None or sig.macd_signal is None else sig.macd - sig.macd_signal,
            "vol_annual": sig.volatility_annual,
            "trend_5": sig.trend_5d,
            "trend_20": sig.trend_20d,
            "dist_sma20": sig.price / sig.sma20 - 1 if sig.sma20 else None,
            "dist_sma50": sig.price / sig.sma50 - 1 if sig.sma50 else None,
        })
    return pd.DataFrame(rows)


def rsi2_signals(ticker: str, df: pd.DataFrame, start: datetime, use_ml: bool = False) -> pd.DataFrame:
    """Возврат к среднему: покупаем глубокий откат в восходящем тренде.

    Вход: RSI(2) < 10 и цена выше SMA200. Выход: закрытие выше SMA5,
    стоп по ATR или таймаут 14 календарных дней.
    """
    close = df["CLOSE"]
    rsi2 = ta.rsi(close, length=2)
    atr = ta.atr(df["HIGH"], df["LOW"], close, length=14)
    out = pd.DataFrame({
        "DATE": df["DATE"],
        "priority": 100.0 - rsi2,
        "score": 100.0 - rsi2,
        "buy": (rsi2 < 10) & (close > close.rolling(200).mean()),
        "sell": False,
        "exit_long": close > close.rolling(5).mean(),
        "exit_short": False,
        "atr_pct": atr / close * 100.0,
        "conf": 0.5,
        "horizon_h": 14 * 24.0,
    })
    out = out[out["DATE"] >= start - timedelta(days=7)]
    return out.dropna(subset=["priority", "atr_pct"])


def momentum_signals(history: Dict[str, pd.DataFrame], start: datetime,
                     lookback: int = 126, top: int = 5, keep: int = 10) -> Dict[str, pd.DataFrame]:
    """Ротация по моментуму: держим бумаги, которые росли сильнее остальных.

    Вход: бумага в топ-`top` по доходности за `lookback` торговых дней и выше
    своей SMA50. Выход: выпала из топ-`keep`, стоп по ATR или таймаут 45 дней.
    """
    closes = pd.DataFrame({t: df.set_index("DATE")["CLOSE"] for t, df in history.items()}).sort_index().ffill(limit=5)
    mom = closes / closes.shift(lookback) - 1
    rank = mom.rank(axis=1, ascending=False)
    above_sma = closes > closes.rolling(50).mean()
    out = {}
    for t, df in history.items():
        atr = ta.atr(df["HIGH"], df["LOW"], df["CLOSE"], length=14)
        atr_pct = pd.Series((atr / df["CLOSE"] * 100.0).values, index=df["DATE"])
        sig = pd.DataFrame({
            "priority": mom[t],
            "score": mom[t],
            "buy": (rank[t] <= top) & above_sma[t],
            "sell": False,
            "exit_long": rank[t] > keep,
            "exit_short": False,
            "atr_pct": atr_pct,
            "conf": 0.5,
            "horizon_h": 45 * 24.0,
        }).loc[df["DATE"]].rename_axis("DATE").reset_index()
        out[t] = sig[sig["DATE"] >= start - timedelta(days=7)].dropna(subset=["priority", "atr_pct"])
    return out


TICKER_STRATEGIES = {"agent": agent_signals, "agent_h1": agent_h1_signals, "rsi2": rsi2_signals}


def _signals_job(job: Tuple[str, str, pd.DataFrame, datetime, bool]) -> Tuple[str, pd.DataFrame]:
    strategy, ticker, df, start, use_ml = job
    return ticker, TICKER_STRATEGIES[strategy](ticker, df, start, use_ml)


# ── Симуляция ───────────────────────────────────────────────────────────────
def by_date(frames: Dict[str, pd.DataFrame], cols: List[str]) -> Dict[datetime, Dict[str, tuple]]:
    out: Dict[datetime, Dict[str, tuple]] = {}
    for t, df in frames.items():
        for row in df[["DATE", *cols]].itertuples(index=False):
            out.setdefault(row[0], {})[t] = tuple(row[1:])
    return out


@dataclass
class Market:
    """Всё, что нужно для прогонов одной стратегии: свечи, сигналы, индекс, LQDT."""
    day_bars: Dict[datetime, Dict[str, tuple]]
    day_sigs: Dict[datetime, Dict[str, tuple]]
    imoex: pd.Series
    lqdt: pd.Series
    intraday: bool = False  # свечи часовые: дата свечи — её начало по Москве


@dataclass
class SimConfig:
    capital: float = 1_000_000.0
    risk: RiskParams = field(default_factory=RiskParams)
    use_take: bool = True
    allow_long: bool = True
    allow_short: bool = False
    min_score: float = 0.0                  # входить, только если |score| не меньше
    skip_entry_hours: Tuple[int, ...] = ()  # (часовые) не входить в эти часы по Москве
    max_new_per_day: int = 3                # как candidates[:3] в tick()
    # Фильтр рынка по IMOEX > SMA(N): дневные — входы только при росте рынка;
    # часовые — лонги при росте, шорты при падении (по закрытию прошлого дня)
    regime_sma: Optional[int] = None
    cooldown_days: int = 0                  # пауза по бумаге после стоп-лосса
    horizon_mult: float = 1.0               # множитель горизонта сделки
    cash_yield: bool = False                # свободные деньги лежат в LQDT
    entry_on_close: bool = False            # стресс-тест: вход на час позже — по закрытию следующей свечи
    random_entry_p: Optional[float] = None  # контрольная группа: вход наугад
    seed: int = 0


class SimPortfolio:
    """Портфель в памяти с тем интерфейсом, который нужен RiskManager."""

    def __init__(self, capital: float):
        self.capital = capital
        self._cash = capital
        self.positions: Dict[str, dict] = {}

    def initial_capital(self) -> float:
        return self.capital

    def cash(self) -> float:
        return self._cash

    def open_positions(self) -> List[dict]:
        return [{"ticker": t, **p} for t, p in self.positions.items()]

    def positions_value(self, prices: Dict[str, float]) -> float:
        total = 0.0
        for t, p in self.positions.items():
            price = prices.get(t, p["entry_price"])
            total += p["qty"] * (2 * p["entry_price"] - price) if p["side"] == "SHORT" else p["qty"] * price
        return total

    def can_afford(self, qty: int, price: float, commission_pct: float = 0.05) -> bool:
        return self._cash >= qty * price * (1 + commission_pct / 100.0)


def _in_session(ts: datetime) -> bool:
    return ts.weekday() < 5 and SESSION_OPEN <= ts.time() < SESSION_CLOSE


def simulate(mkt: Market, start: datetime, end: Optional[datetime], cfg: SimConfig):
    rng = random.Random(cfg.seed)
    pf = SimPortfolio(cfg.capital)
    risk = RiskManager(pf, cfg.risk)
    comm = cfg.risk.commission_pct / 100.0
    # Дневная свеча: вход ~10:00, закрытие ~19:00. Часовая: от начала до +1 ч.
    entry_offset = timedelta(0) if mkt.intraday else timedelta(hours=10)
    bar_end = timedelta(hours=1) if mkt.intraday else timedelta(hours=19)
    regime = regime_days = regime_up = None
    if cfg.regime_sma:
        up = mkt.imoex > mkt.imoex.rolling(cfg.regime_sma).mean()
        regime = up.to_dict()
        regime_days, regime_up = list(up.index), list(up.values)

    def market_up(d: datetime) -> Optional[bool]:
        """Часовые: режим по закрытию предыдущего торгового дня — без заглядывания вперёд."""
        i = bisect.bisect_left(regime_days, d.replace(hour=0, minute=0, second=0)) - 1
        return bool(regime_up[i]) if i >= 0 else None
    lqdt_ret = mkt.lqdt.pct_change().fillna(0.0).to_dict() if cfg.cash_yield else {}
    cash_yield_day = None  # часовые свечи: начисляем раз в день, на первой свече дня
    blocked_until: Dict[str, datetime] = {}
    last_close: Dict[str, float] = {}
    pending: List[Tuple[str, dict, str]] = []
    trades, equity = [], []

    for d in sorted(x for x in mkt.day_bars if x >= start and (end is None or x <= end)):
        bars = mkt.day_bars[d]
        sigs = {t: dict(zip(SIG_COLS, v)) for t, v in mkt.day_sigs.get(d, {}).items()}
        day = d.replace(hour=0, minute=0, second=0)
        if day != cash_yield_day:
            pf._cash *= 1 + lqdt_ret.get(day, 0.0)
            cash_yield_day = day

        # 1. Входы по сигналам прошлой свечи — по цене открытия этой
        if mkt.intraday and not _in_session(d):
            pending = []
        for t, s, side in pending:
            if t in pf.positions or t not in bars:
                continue
            if not risk.can_open_new()[0]:
                break
            price = bars[t][3] if cfg.entry_on_close else bars[t][0]
            plan = risk.plan_trade(t, price, s["atr_pct"], political_risk=0.0, confidence=s["conf"], side=side)
            if plan is None or plan.qty <= 0:
                continue
            pf._cash -= plan.qty * price * (1 + comm)
            no_take = math.inf if side == "LONG" else -math.inf
            pf.positions[t] = {
                "side": side,
                "qty": plan.qty,
                "entry_price": price,
                "sl": plan.stop_loss,
                "tp": plan.take_profit if cfg.use_take else no_take,
                "entry_date": d,
                "deadline": d + entry_offset + timedelta(hours=s["horizon_h"] * cfg.horizon_mult),
                # вход по закрытию: максимум и минимум этой свечи были до входа
                "skip_bar": d if cfg.entry_on_close else None,
            }
        pending = []

        # 2. Выходы: гэп/SL/TP по OHLC свечи, таймаут и смена сигнала — по закрытию
        for t in list(pf.positions):
            if t not in bars:
                continue
            o, h, l, c = bars[t]
            p = pf.positions[t]
            if p.get("skip_bar") == d:
                continue
            is_long = p["side"] == "LONG"
            # «хуже» и «лучше» для позиции: для шорта рост цены — это убыток
            worst, best = (l, h) if is_long else (h, l)
            beyond = (lambda x, lvl: x <= lvl) if is_long else (lambda x, lvl: x >= lvl)
            reached = (lambda x, lvl: x >= lvl) if is_long else (lambda x, lvl: x <= lvl)
            if beyond(o, p["sl"]):
                price, reason = o, "SL"
            elif reached(o, p["tp"]):
                price, reason = o, "TP"
            elif beyond(worst, p["sl"]):
                price, reason = p["sl"], "SL"
            elif reached(best, p["tp"]):
                price, reason = p["tp"], "TP"
            elif d + bar_end >= p["deadline"]:
                price, reason = c, "TIMEOUT"
            elif t in sigs and sigs[t]["exit_long" if is_long else "exit_short"]:
                price, reason = c, "SIGNAL"
            else:
                continue
            qty, entry = p["qty"], p["entry_price"]
            cost = qty * entry * (1 + comm)
            gross = qty * price if is_long else qty * (2 * entry - price)
            proceeds = gross - qty * price * comm
            pf._cash += proceeds
            trades.append({
                "ticker": t,
                "side": p["side"],
                "entry_date": p["entry_date"],
                "exit_date": d,
                "qty": qty,
                "entry": entry,
                "exit": price,
                "reason": reason,
                "pnl_abs": proceeds - cost,
                "pnl_pct": (proceeds / cost - 1) * 100.0,
                "days": (d - p["entry_date"]).total_seconds() / 86400,
            })
            del pf.positions[t]
            if reason == "SL" and cfg.cooldown_days:
                blocked_until[t] = d + timedelta(days=cfg.cooldown_days)

        # 3. Оценка портфеля по закрытию
        for t, bar in bars.items():
            last_close[t] = bar[3]
        equity.append((d, pf._cash + pf.positions_value(last_close)))

        # 4. Кандидаты на следующую свечу
        allow_long, allow_short = cfg.allow_long, cfg.allow_short
        if regime is not None:
            if not mkt.intraday:
                if not regime.get(d, False):
                    continue
            else:
                up = market_up(d)
                allow_long = allow_long and up is True
                allow_short = allow_short and up is False
        if mkt.intraday and cfg.skip_entry_hours and (d + timedelta(hours=1)).hour in cfg.skip_entry_hours:
            continue
        cands = []
        for t, s in sigs.items():
            if t in pf.positions or d < blocked_until.get(t, d):
                continue
            if cfg.random_entry_p is not None:
                if rng.random() < cfg.random_entry_p:
                    side = rng.choice(["LONG", "SHORT"]) if cfg.allow_short else "LONG"
                    horizon = 12.0 if mkt.intraday else 72.0
                    cands.append((rng.random(), t, {**s, "conf": 0.4, "horizon_h": horizon}, side))
                continue
            if abs(s["score"]) < cfg.min_score:
                continue
            if allow_long and s["buy"]:
                cands.append((s["priority"], t, s, "LONG"))
            elif allow_short and s["sell"]:
                cands.append((s["priority"], t, s, "SHORT"))
        cands.sort(key=lambda x: x[0], reverse=True)
        pending = [(t, s, side) for _, t, s, side in cands[:cfg.max_new_per_day]]

    return trades, equity


# ── Отчёт ───────────────────────────────────────────────────────────────────
def _curve_stats(eq: pd.Series, capital: float) -> dict:
    years = max((eq.index[-1] - eq.index[0]).days / 365.25, 1e-9)
    total = eq.iloc[-1] / capital - 1
    return {
        "total": total * 100,
        "cagr": ((1 + total) ** (1 / years) - 1) * 100,
        "maxdd": (eq / eq.cummax() - 1).min() * 100,
    }


def trade_stats(trades: List[dict]) -> dict:
    row = {"trades": len(trades), "winrate": 0.0, "avg_win": 0.0, "avg_loss": 0.0,
           "exp": 0.0, "pf": 0.0, "days": 0.0}
    if trades:
        tr = pd.DataFrame(trades)
        wins, losses = tr[tr.pnl_abs > 0], tr[tr.pnl_abs <= 0]
        row.update(
            winrate=len(wins) / len(tr) * 100,
            avg_win=wins.pnl_pct.mean() if len(wins) else 0.0,
            avg_loss=losses.pnl_pct.mean() if len(losses) else 0.0,
            exp=tr.pnl_pct.mean(),
            pf=wins.pnl_abs.sum() / -losses.pnl_abs.sum() if losses.pnl_abs.sum() < 0 else math.inf,
            days=tr.days.mean(),
        )
    return row


def summarize(name: str, trades: List[dict], equity: List[tuple], capital: float) -> dict:
    eq = pd.Series([v for _, v in equity], index=[d for d, _ in equity])
    return {"name": name, **trade_stats(trades), **_curve_stats(eq, capital)}


def hold_row(name: str, series: pd.Series, start: datetime, end: Optional[datetime]) -> dict:
    s = series[(series.index >= start) & ((series.index <= end) if end else True)]
    return {"name": name, "trades": None, **_curve_stats(s, s.iloc[0])}


HEADER = (f"{'':<34}{'сделок':>7}{'winrate':>9}{'ср.плюс':>9}{'ср.минус':>9}"
          f"{'на сделку':>10}{'PF':>6}{'итого':>9}{'в год':>8}{'просадка':>10}{'дней':>6}")


def fmt(r: dict) -> str:
    tail = f"{r['total']:>+8.1f}%{r['cagr']:>+7.1f}%{r['maxdd']:>+9.1f}%" if "total" in r else f"{'':>34}"
    if r["trades"] is None:
        return f"{r['name']:<34}{'':>50}{tail}"
    return (f"{r['name']:<34}{r['trades']:>7}{r['winrate']:>8.1f}%{r['avg_win']:>+8.2f}%"
            f"{r['avg_loss']:>+8.2f}%{r['exp']:>+9.2f}%{r['pf']:>6.2f}{tail}{r['days']:>6.1f}")


def print_benchmarks(mkt: Market, start: datetime, end: Optional[datetime]) -> None:
    print(fmt(hold_row("IMOEX: купил и держал", mkt.imoex, start, end)))
    if not mkt.lqdt.empty:
        print(fmt(hold_row("LQDT: деньги в фонде денежного рынка", mkt.lqdt, start, end)))


def random_baseline(mkt: Market, start: datetime, end: Optional[datetime], base: SimConfig, seeds: int) -> dict:
    """Те же выходы и размер позиций, но вход наугад с той же частотой,
    с какой стратегия даёт сигналы на вход. Медиана по `seeds` прогонам."""
    rows = [v for d, day in mkt.day_sigs.items() if d >= start and (end is None or d <= end) for v in day.values()]
    buy, sell = SIG_COLS.index("buy"), SIG_COLS.index("sell")
    entry_share = sum(1 for v in rows if v[buy] or (base.allow_short and v[sell])) / max(len(rows), 1)
    runs = []
    for seed in range(seeds):
        cfg = replace(base, random_entry_p=entry_share, seed=seed)
        runs.append(summarize("", *simulate(mkt, start, end, cfg), cfg.capital))
    out = {k: statistics.median(r[k] for r in runs) for k in runs[0] if k != "name"}
    out["name"] = f"случайный вход (медиана {seeds})"
    out["trades"] = int(out["trades"])
    return out


# ── Сценарии ────────────────────────────────────────────────────────────────
def _signals_cache(strategy: str, ticker: str, use_ml: bool, interval: int) -> Tuple[Path, Path]:
    """Файл кэша сигналов и файл истории, из которой он посчитан."""
    suffix = "" if interval == DAILY else f"_m{interval}"
    name = f"{strategy}{'_ml' if use_ml else ''}_{ticker}.csv"
    return SIGNALS_DIR / name, HISTORY_DIR / f"{ticker}{suffix}.csv"


def build_markets(strategies: List[str], history: Dict[str, pd.DataFrame], imoex: pd.Series,
                  lqdt: pd.Series, start: datetime, workers: int, use_ml: bool,
                  intraday: bool = False, use_cache: bool = True) -> Dict[str, Market]:
    day_bars = by_date(history, ["OPEN", "HIGH", "LOW", "CLOSE"])
    interval = HOURLY if intraday else DAILY
    markets = {}
    for strategy in strategies:
        if strategy in TICKER_STRATEGIES:
            # Сигналы считаются долго (часовые — десятки минут), поэтому кэшируются
            # на диск и пересчитываются, только если история свежее кэша.
            signals, todo = {}, []
            for t, df in history.items():
                cache, source = _signals_cache(strategy, t, use_ml, interval)
                if use_cache and cache.exists() and cache.stat().st_mtime >= source.stat().st_mtime:
                    signals[t] = pd.read_csv(cache, parse_dates=["DATE"])
                else:
                    todo.append((strategy, t, df, start, use_ml))
            print(f"Сигналы «{strategy}»: из кэша {len(signals)}, считаю {len(todo)} бумаг…", flush=True)
            if todo:
                SIGNALS_DIR.mkdir(parents=True, exist_ok=True)
                with ProcessPoolExecutor(max_workers=workers) as pool:
                    for t, sig in pool.map(_signals_job, todo):
                        sig.to_csv(_signals_cache(strategy, t, use_ml, interval)[0], index=False)
                        signals[t] = sig
        else:
            name, lookback = strategy.split(":") if ":" in strategy else (strategy, "126")
            signals = momentum_signals(history, start, lookback=int(lookback))
        day_sigs = by_date({t: s for t, s in signals.items() if not s.empty}, SIG_COLS)
        markets[strategy] = Market(day_bars, day_sigs, imoex, lqdt, intraday=intraday)
    return markets


def h1(**overrides) -> SimConfig:
    """Настройки часовой стратегии как в проде, с заменой отдельных параметров."""
    return SimConfig(**{"risk": INTRADAY_RISK, "allow_short": True, **overrides})


# Гипотезы для часовой стратегии — заданы заранее, а не подобраны по результату.
# Изменение принимаем, только если оно лучше и на 2023–2024, и на 2025–2026.
H1_EXPERIMENTS = [
    ("текущая стратегия", h1()),
    # Стоп срабатывает вдвое чаще тейка — возможно, он стоит в рыночном шуме
    ("стоп ATR×3, тейк ATR×4,5", h1(risk=replace(INTRADAY_RISK, atr_stop_mult=3, atr_take_mult=4.5))),
    ("стоп ATR×4, тейк ATR×6", h1(risk=replace(INTRADAY_RISK, atr_stop_mult=4, atr_take_mult=6))),
    ("стоп ATR×2, тейк ATR×2", h1(risk=replace(INTRADAY_RISK, atr_stop_mult=2, atr_take_mult=2))),
    ("стоп ATR×3, без тейка", h1(risk=replace(INTRADAY_RISK, atr_stop_mult=3), use_take=False)),
    ("стоп 4%, без тейка", h1(risk=replace(INTRADAY_RISK, atr_stop_mult=50), use_take=False)),
    # Меньше сделок — меньше комиссий
    ("порог |скор| ≥ 2,5", h1(min_score=2.5)),
    ("порог |скор| ≥ 3", h1(min_score=3.0)),
    ("порог |скор| ≥ 3,5", h1(min_score=3.5)),
    # Время входа и стороны
    ("без входов в 13 и 15 ч", h1(skip_entry_hours=(13, 15))),
    ("только шорты", h1(allow_long=False)),
    ("только лонги", h1(allow_short=False)),
    ("лонг при IMOEX>SMA100, шорт ниже", h1(regime_sma=100)),
    ("лонг при IMOEX>SMA50, шорт ниже", h1(regime_sma=50)),
    # Длительность сделки
    ("горизонт ×0,5", h1(horizon_mult=0.5)),
    ("горизонт ×2", h1(horizon_mult=2.0)),
]

H1_HEADER = (f"{'':<34}{'2023–2024: сделок':>18}{'win':>7}{'PF':>6}{'итого':>9}"
             f"{'2025–2026: сделок':>20}{'win':>7}{'PF':>6}{'итого':>9}")


def fmt_pair(label: str, a: dict, b: dict) -> str:
    cell = lambda r: f"{r['trades']:>18}{r['winrate']:>6.1f}%{r['pf']:>6.2f}{r['total']:>+8.1f}%"
    return f"{label:<34}{cell(a)}  {cell(b)}"


def run_h1_experiments(mkt: Market, seeds: int) -> None:
    (is_start, is_end), (oos_start, oos_end) = PERIODS["is"], PERIODS["oos"]
    print(H1_HEADER)
    for label, cfg in H1_EXPERIMENTS:
        a = summarize("", *simulate(mkt, is_start, is_end, cfg), cfg.capital)
        b = summarize("", *simulate(mkt, oos_start, oos_end, cfg), cfg.capital)
        print(fmt_pair(label, a, b), flush=True)
    base = h1()
    print(fmt_pair(f"случайный вход (медиана {seeds})",
                   random_baseline(mkt, is_start, is_end, base, seeds),
                   random_baseline(mkt, oos_start, oos_end, base, seeds)))


# Эксперименты дневной версии: (стратегия, подпись, настройки). Решения
# принимаются по периоду подбора (is), период проверки (oos) только подтверждает.
MOMENTUM_RISK = RiskParams(atr_stop_mult=3.0)
EXPERIMENTS = [
    ("agent", "агент как в проде", SimConfig()),
    ("agent", "+ вход при IMOEX > SMA50", SimConfig(regime_sma=50)),
    ("agent", "+ вход при IMOEX > SMA100", SimConfig(regime_sma=100)),
    ("agent", "+ вход при IMOEX > SMA200", SimConfig(regime_sma=200)),
    ("agent", "+ пауза 5 дней после SL", SimConfig(cooldown_days=5)),
    ("agent", "горизонт ×2", SimConfig(horizon_mult=2.0)),
    ("agent", "горизонт ×3, без тейка", SimConfig(horizon_mult=3.0, use_take=False)),
    ("agent", "SMA100 + горизонт ×3 без тейка", SimConfig(regime_sma=100, horizon_mult=3.0, use_take=False)),
    ("agent", "SMA100 + гор. ×3 + пауза 5 дн.",
     SimConfig(regime_sma=100, horizon_mult=3.0, use_take=False, cooldown_days=5)),
    ("rsi2", "RSI(2) откат в тренде", SimConfig(use_take=False, risk=MOMENTUM_RISK)),
    ("rsi2", "RSI(2) + IMOEX > SMA100", SimConfig(use_take=False, risk=MOMENTUM_RISK, regime_sma=100)),
    ("momentum", "моментум 6 мес., топ-5", SimConfig(use_take=False, risk=MOMENTUM_RISK)),
    ("momentum", "моментум 6 мес. + IMOEX > SMA100", SimConfig(use_take=False, risk=MOMENTUM_RISK, regime_sma=100)),
    ("momentum:63", "моментум 3 мес., топ-5", SimConfig(use_take=False, risk=MOMENTUM_RISK)),
    ("momentum:63", "моментум 3 мес. + IMOEX > SMA100",
     SimConfig(use_take=False, risk=MOMENTUM_RISK, regime_sma=100)),
    ("momentum:252", "моментум 12 мес., топ-5", SimConfig(use_take=False, risk=MOMENTUM_RISK)),
]


def run_research(markets: Dict[str, Market], start: datetime, end: Optional[datetime], cash_yield: bool) -> None:
    print(HEADER)
    for strategy, label, cfg in EXPERIMENTS:
        cfg = replace(cfg, cash_yield=cash_yield)
        print(fmt(summarize(label, *simulate(markets[strategy], start, end, cfg), cfg.capital)), flush=True)
    print_benchmarks(next(iter(markets.values())), start, end)


def run_intraday(mkt: Market, base: SimConfig, seeds: int) -> List[dict]:
    """Текущий агент по периодам: подбор, проверка и всё вместе."""
    all_trades: List[dict] = []
    for period in ("is", "oos", "all"):
        start, end = PERIODS[period]
        trades, equity = simulate(mkt, start, end, base)
        print(f"\n— {period}: {start:%Y-%m-%d} — {(end or max(mkt.day_bars)):%Y-%m-%d}")
        print(HEADER)
        print(fmt(summarize("агент: лонг+шорт, до 24 ч", trades, equity, base.capital)))
        for side in ("LONG", "SHORT"):
            print(fmt({"name": f"  из них {side}", **trade_stats([t for t in trades if t["side"] == side])}))
        print(fmt(random_baseline(mkt, start, end, base, seeds)), flush=True)
        print_benchmarks(mkt, start, end)
        if period == "all":
            all_trades = trades
    return all_trades


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--strategy", default="agent", help="agent, agent_h1, rsi2, momentum или momentum:<дней>")
    ap.add_argument("--tickers", help="свой список бумаг через запятую вместо пула агента (PRIORITY_TICKERS)")
    ap.add_argument("--period", choices=sorted(PERIODS), default="all",
                    help="is — подбор 2023–2024, oos — проверка с 2025, all — всё")
    ap.add_argument("--research", action="store_true", help="прогнать все дневные эксперименты из EXPERIMENTS")
    ap.add_argument("--experiments", action="store_true", help="(agent_h1) прогнать гипотезы из H1_EXPERIMENTS")
    ap.add_argument("--no-cache", action="store_true", help="пересчитать сигналы, не читая кэш data/signals/")
    ap.add_argument("--prepare-only", action="store_true",
                    help="только обновить историю и кэш сигналов (для переобучения ML), без прогонов")
    ap.add_argument("--sweep", action="store_true", help="сетка множителей SL/TP")
    ap.add_argument("--cash-yield", action="store_true", help="свободные деньги лежат в LQDT")
    ap.add_argument("--ml", action="store_true", help="включить ML-прогноз в скоринг агента (долго)")
    ap.add_argument("--random-seeds", type=int, default=20, help="прогонов случайного входа")
    ap.add_argument("--refresh", action="store_true", help="перекачать историю с MOEX")
    ap.add_argument("--workers", type=int, default=4)
    args = ap.parse_args()

    start, end = PERIODS[args.period]
    now = datetime.now()
    intraday = args.strategy == "agent_h1"
    interval = HOURLY if intraday else DAILY

    universe = [t.strip().upper() for t in args.tickers.split(",") if t.strip()] if args.tickers else UNIVERSE
    history = {}
    for t in universe:
        df = load_history(t, now, refresh=args.refresh, interval=interval)
        if df.empty:
            print(f"! {t}: нет данных на MOEX")
        else:
            history[t] = df
    imoex = load_history("IMOEX", now, refresh=args.refresh).set_index("DATE")["CLOSE"]
    if intraday and args.refresh:
        load_history("IMOEX", now, refresh=True, interval=HOURLY)  # для ML-признаков (research/price_ml.py)
    lqdt = load_lqdt(now, refresh=args.refresh)

    strategies = sorted({s for s, _, _ in EXPERIMENTS}) if args.research else [args.strategy]
    markets = build_markets(strategies, history, imoex, lqdt, PERIODS["all"][0], args.workers, args.ml,
                            intraday, use_cache=not args.no_cache)
    if args.prepare_only:
        print("История и сигналы обновлены.")
        return

    if intraday:
        if args.experiments:
            run_h1_experiments(markets[args.strategy], args.random_seeds)
            return
        base = h1(cash_yield=args.cash_yield)
        trades = run_intraday(markets[args.strategy], base, args.random_seeds)
    else:
        period_end = end or max(next(iter(markets.values())).day_bars)
        print(f"Период: {start:%Y-%m-%d} — {period_end:%Y-%m-%d}"
              + (", свободные деньги в LQDT" if args.cash_yield else "") + "\n")
        if args.research:
            run_research(markets, start, end, args.cash_yield)
            return
        mkt = markets[args.strategy]
        base = SimConfig(cash_yield=args.cash_yield)
        if args.strategy != "agent":
            base = replace(base, use_take=False, risk=MOMENTUM_RISK)
        trades, equity = simulate(mkt, start, end, base)
        print(HEADER)
        print(fmt(summarize(f"{args.strategy}" + (" (как в проде)" if args.strategy == "agent" else ""),
                            trades, equity, base.capital)))
        print(fmt(random_baseline(mkt, start, end, base, args.random_seeds)))
        print_benchmarks(mkt, start, end)

    if trades:
        tr = pd.DataFrame(trades)
        print("\nПричины выхода:")
        for reason, g in tr.groupby("reason"):
            print(f"  {reason:<8} {len(g):>5} сделок, winrate {(g.pnl_abs > 0).mean() * 100:5.1f}%, "
                  f"в среднем {g.pnl_pct.mean():+.2f}%")
        out = BASE_DIR / "data" / f"backtest_trades_{args.strategy.replace(':', '_')}.csv"
        tr.to_csv(out, index=False)
        print(f"\nСделки: {out.relative_to(BASE_DIR)}")

    if args.sweep and not intraday:
        print("\nСетка SL/TP (стоп = ATR × k, тейк = стоп × R):")
        print(HEADER)
        for k in (1.0, 2.0, 3.0, 4.0):
            for ratio in (0.25, 0.5, 1.0, 1.5, 2.0):
                cfg = replace(base, use_take=True, risk=replace(base.risk, atr_stop_mult=k, atr_take_mult=k * ratio))
                print(fmt(summarize(f"k={k:g} R={ratio:g}", *simulate(mkt, start, end, cfg), cfg.capital)))


if __name__ == "__main__":
    main()
