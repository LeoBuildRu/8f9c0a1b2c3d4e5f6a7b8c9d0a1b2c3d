"""Сверка признаков: обучение (кэш сигналов + история) против живого агента.

Для нескольких бумаг берём свечу из обучающей выборки и считаем признаки так,
как это делает агент в торговле: свежая загрузка свечей с MOEX за 30 дней,
MarketAnalyzer, feature_row. Расхождения означали бы, что модель в торговле
видит не то, на чём училась.

    python research/parity_check.py --bar "2026-10-01 15:00"
"""

from __future__ import annotations

import argparse
import sys
from datetime import timedelta
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import backtest as bt  # noqa: E402
from app.agent.market_analyzer import MarketAnalyzer  # noqa: E402
from app.agent.ml_model import FEATURES, feature_row  # noqa: E402
from app.agent.trader_agent import LOOKBACK_DAYS  # noqa: E402
from app.data_services import fetch_moex_data  # noqa: E402
from research.price_ml import build_dataset  # noqa: E402


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--bar", default="2026-10-01 15:00")
    ap.add_argument("--tickers", default="SBER,GAZP,OZON")
    args = ap.parse_args()
    bar = pd.Timestamp(args.bar)

    train = build_dataset(6).set_index(["DATE", "ticker"])
    end = (bar + timedelta(hours=2)).to_pydatetime()
    imoex = fetch_moex_data("IMOEX", end - timedelta(days=LOOKBACK_DAYS + 5), end, 60).set_index("DATE")
    imoex = imoex[imoex.index <= bar]
    worst = 0.0
    for t in args.tickers.split(","):
        df = fetch_moex_data(t, end - timedelta(days=LOOKBACK_DAYS + 5), end, 60)
        window = df[(df.DATE > bar - timedelta(days=LOOKBACK_DAYS)) & (df.DATE <= bar)].reset_index(drop=True)
        sig = MarketAnalyzer({t: 0}, lookback_days=LOOKBACK_DAYS, interval=bt.HOURLY).analyze_ticker(t, df=window)
        bars = df[df.DATE <= bar].set_index("DATE")
        live = feature_row(sig, bars, imoex).iloc[0]
        ref = train.loc[(bar, t), FEATURES].astype(float).fillna(0)
        diff = (live - ref).abs()
        worst = max(worst, float(diff.max()))
        print(f"{t}: максимальное расхождение {diff.max():.2e} в {diff.idxmax()}")
        if diff.max() > 1e-6:
            print(pd.DataFrame({"обучение": ref, "агент": live, "разница": diff}).round(5).to_string())
    print("ПРИЗНАКИ СОВПАДАЮТ" if worst < 1e-6 else "ЕСТЬ РАСХОЖДЕНИЯ")


if __name__ == "__main__":
    main()
