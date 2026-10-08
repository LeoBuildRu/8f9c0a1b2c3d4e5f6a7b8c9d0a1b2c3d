"""Новостные признаки для ML-прогноза цены (research/price_ml.py --news).

Для каждой свечи с сигналом агента (DATE по Москве, ticker) — новости за
предыдущие 24 часа, опубликованные до начала свечи: сколько их было по
компании и её отрасли, их средняя оценка выбранным методом, и то же по
новостям рынка в целом. Источник — data/news_eval/pairs.pkl из news_eval.py.

    python research/news_features.py --method fin_tiny
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import backtest as bt  # noqa: E402
from research.news_eval import MSK, OUT  # noqa: E402

WINDOW = pd.Timedelta(hours=24)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--method", default="fin_tiny", help="колонка оценки из pairs.pkl")
    args = ap.parse_args()

    pairs = pd.read_pickle(OUT / "pairs.pkl")
    pairs["t"] = pairs.date + MSK  # публикация по Москве — как время свечей
    score = pairs[args.method]
    own = pairs[pairs.scope != "market"].assign(s=score)
    market = pairs[pairs.scope == "market"].drop_duplicates("id").assign(s=score)
    m_times, m_scores = market.t.values, market.s.fillna(0).values
    m_order = np.argsort(m_times)
    m_times, m_scores = m_times[m_order], m_scores[m_order]

    rows = []
    for t in bt.UNIVERSE:
        cache = bt.SIGNALS_DIR / f"agent_h1_{t}.csv"
        if not cache.exists():
            continue
        dates = pd.read_csv(cache, usecols=["DATE"], parse_dates=["DATE"]).DATE.values
        news = own[own.ticker == t].sort_values("t")
        n_times, n_scores = news.t.values, news.s.fillna(0).values
        # Признаки на момент закрытия свечи: публикации до её конца (+1 ч) за прошлые сутки
        end = dates + np.timedelta64(1, "h")
        lo_n, hi_n = np.searchsorted(n_times, end - WINDOW), np.searchsorted(n_times, end)
        lo_m, hi_m = np.searchsorted(m_times, end - WINDOW), np.searchsorted(m_times, end)
        cs_n = np.concatenate([[0], np.cumsum(n_scores)])
        cs_m = np.concatenate([[0], np.cumsum(m_scores)])
        cnt_n, cnt_m = hi_n - lo_n, hi_m - lo_m
        rows.append(pd.DataFrame({
            "DATE": dates,
            "ticker": t,
            "news_n_24h": cnt_n,
            "news_s_24h": np.where(cnt_n > 0, (cs_n[hi_n] - cs_n[lo_n]) / np.maximum(cnt_n, 1), 0.0),
            "market_n_24h": cnt_m,
            "market_s_24h": np.where(cnt_m > 0, (cs_m[hi_m] - cs_m[lo_m]) / np.maximum(cnt_m, 1), 0.0),
        }))
    out = pd.concat(rows)
    path = OUT / f"features_{args.method}.pkl"
    out.to_pickle(path)
    print(f"{len(out)} строк, доля свечей с новостями по бумаге: {(out.news_n_24h > 0).mean():.1%} → {path}")


if __name__ == "__main__":
    main()
