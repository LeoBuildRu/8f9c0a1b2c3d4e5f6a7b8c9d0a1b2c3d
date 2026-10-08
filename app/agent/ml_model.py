"""ML-прогноз направления цены для часовой стратегии агента.

Модель LightGBM предсказывает вероятность роста цены за следующие 6 часовых
свечей. Агент открывает лонг, когда вероятность выше верхнего порога, и шорт —
когда ниже нижнего (пороги — 10% самых уверенных прогнозов). Проверено в
research/price_ml.py и research/ml_validate.py: обучение на 2023–2024 дало
PF 1,18 на 2025–2026, где модель данных не видела.

Признаки здесь общие для обучения и живой торговли — расчёт не дублируется.
Обучение: research/train_ml.py; модель лежит в data/models/ (не в git).
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional

import numpy as np
import pandas as pd

# Признаки сигнала агента (TickerSignal) — те же колонки, что в кэше сигналов бэктеста
SIGNAL_FEATURES = ["score", "rsi", "macd_hist", "atr_pct", "vol_annual", "trend_5", "trend_20",
                   "dist_sma20", "dist_sma50"]
BAR_FEATURES = ["ret_1", "ret_3", "ret_6", "ret_24", "vol_z", "imoex_ret_1", "imoex_ret_6", "imoex_ret_24"]
TIME_FEATURES = ["hour", "weekday"]
FEATURES = SIGNAL_FEATURES + BAR_FEATURES + TIME_FEATURES

DEFAULT_MODEL_DIR = Path(__file__).resolve().parent.parent.parent / "data" / "models"
MODEL_NAME = "ml_h6"


def bar_features(bars: pd.DataFrame, imoex: pd.DataFrame) -> pd.DataFrame:
    """Признаки по часовым свечам бумаги и IMOEX; индекс — начало свечи (Москва)."""
    bars = bars.sort_index()
    f = pd.DataFrame(index=bars.index)
    for k in (1, 3, 6, 24):
        f[f"ret_{k}"] = bars.CLOSE.pct_change(k) * 100
    f["vol_z"] = (bars.VOL - bars.VOL.rolling(20).mean()) / bars.VOL.rolling(20).std()
    imoex_close = imoex.sort_index().CLOSE
    for k in (1, 6, 24):
        f[f"imoex_ret_{k}"] = imoex_close.pct_change(k).reindex(bars.index, method="ffill") * 100
    return f


def signal_features(sig: Any) -> Dict[str, Optional[float]]:
    """Признаки из TickerSignal — так же, как они пишутся в кэш сигналов бэктеста."""
    return {
        "score": sig.score,
        "rsi": sig.rsi,
        "macd_hist": None if sig.macd is None or sig.macd_signal is None else sig.macd - sig.macd_signal,
        "atr_pct": sig.atr_pct,
        "vol_annual": sig.volatility_annual,
        "trend_5": sig.trend_5d,
        "trend_20": sig.trend_20d,
        "dist_sma20": sig.price / sig.sma20 - 1 if sig.sma20 else None,
        "dist_sma50": sig.price / sig.sma50 - 1 if sig.sma50 else None,
    }


def feature_row(sig: Any, bars: pd.DataFrame, imoex: pd.DataFrame) -> pd.DataFrame:
    """Одна строка признаков на последнюю закрытую свечу бумаги."""
    bf = bar_features(bars, imoex).iloc[-1].to_dict()
    when = bars.index[-1]
    row = {**signal_features(sig), **bf, "hour": when.hour, "weekday": when.weekday()}
    return pd.DataFrame([row], columns=FEATURES).astype(float).fillna(0)


@dataclass
class MLModel:
    booster: Any
    features: List[str]
    lo: float           # шорт, если вероятность роста не выше
    hi: float           # лонг, если вероятность роста не ниже
    meta: Dict[str, Any]

    @classmethod
    def load(cls, model_dir: Path = DEFAULT_MODEL_DIR, name: str = MODEL_NAME) -> Optional["MLModel"]:
        meta_path, model_path = model_dir / f"{name}.json", model_dir / f"{name}.txt"
        if not meta_path.exists() or not model_path.exists():
            return None
        import lightgbm as lgb
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        return cls(lgb.Booster(model_file=str(model_path)), meta["features"], meta["lo"], meta["hi"], meta)

    def save(self, model_dir: Path = DEFAULT_MODEL_DIR, name: str = MODEL_NAME) -> None:
        model_dir.mkdir(parents=True, exist_ok=True)
        self.booster.save_model(str(model_dir / f"{name}.txt"))
        meta = {**self.meta, "features": self.features, "lo": self.lo, "hi": self.hi}
        (model_dir / f"{name}.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")

    def prob_up(self, X: pd.DataFrame) -> np.ndarray:
        return self.booster.predict(X[self.features].astype(float).fillna(0).values)

    def side(self, p: float) -> Optional[str]:
        if p >= self.hi:
            return "LONG"
        if p <= self.lo:
            return "SHORT"
        return None
