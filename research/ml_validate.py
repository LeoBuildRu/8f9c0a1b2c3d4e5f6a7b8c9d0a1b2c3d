"""Стресс-тесты стратегии «только ML» (LightGBM, горизонт 6 ч, крайние прогнозы).

Выбор сделан по 2023–2024; здесь проверяем, насколько результат устойчив:
по годам и сторонам, при больших издержках, при входе на час позже, против
случайного входа с той же частотой и по бумагам.

    python research/ml_validate.py --horizon 6 --share 0.10
    python research/ml_validate.py --tickers SBER,GAZP,...,NLMK   # свой пул бумаг
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import replace
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import backtest as bt  # noqa: E402
from app.agent.ml_model import FEATURES  # noqa: E402
from research.price_ml import SPLIT, build_dataset, ml_market, models, oof_predict  # noqa: E402


def side_year_table(trades: list) -> str:
    if not trades:
        return "  сделок нет"
    tr = pd.DataFrame(trades)
    tr["year"] = pd.to_datetime(tr.entry_date).dt.year
    rows = []
    for key, g in list(tr.groupby("year")) + list(tr.groupby("side")):
        w, l = g[g.pnl_abs > 0], g[g.pnl_abs <= 0]
        rows.append(f"  {str(key):<6} сделок {len(g):>5}, прибыльных {len(w) / len(g) * 100:5.1f}%, "
                    f"PF {w.pnl_abs.sum() / -l.pnl_abs.sum():.2f}, итог {g.pnl_abs.sum():>+12,.0f} ₽")
    return "\n".join(rows)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--horizon", type=int, default=6)
    ap.add_argument("--share", type=float, default=0.10)
    ap.add_argument("--news", help="pkl с новостными признаками")
    ap.add_argument("--tickers", help="свой пул бумаг через запятую (по умолчанию — пул агента)")
    ap.add_argument("--workers", type=int, default=3, help="процессов для расчёта сигналов новых бумаг")
    args = ap.parse_args()
    tickers = [t.strip().upper() for t in args.tickers.split(",") if t.strip()] if args.tickers else bt.UNIVERSE

    # Сначала история и сигналы: для новых бумаг они считаются и попадают в кэш
    now = pd.Timestamp.now().to_pydatetime()
    history = {t: df for t in tickers if not (df := bt.load_history(t, now, interval=bt.HOURLY)).empty}
    missing = sorted(set(tickers) - set(history))
    if missing:
        print(f"Нет данных на MOEX: {', '.join(missing)}", flush=True)
    imoex = bt.load_history("IMOEX", now).set_index("DATE")["CLOSE"]
    lqdt = bt.load_lqdt(now)
    base = bt.build_markets(["agent_h1"], history, imoex, lqdt, bt.PERIODS["all"][0], args.workers, False, True)["agent_h1"]

    data = build_dataset(args.horizon, list(history))
    features = list(FEATURES)
    if args.news:
        news = pd.read_pickle(args.news)
        data = data.merge(news, on=["DATE", "ticker"], how="left")
        cols = [c for c in news.columns if c not in ("DATE", "ticker")]
        data[cols] = data[cols].fillna(0)
        features += cols
    X = data[features].astype(float).fillna(0)
    y = (data.fwd > 0).astype(int).values
    is_mask = (data.DATE < SPLIT).values

    model = models()["LightGBM"]
    oof = oof_predict(model, X[is_mask], y[is_mask], data.DATE[is_mask])
    final = model.fit(X[is_mask], y[is_mask])
    prob = pd.Series(np.nan, index=data.index)
    prob[is_mask] = oof
    prob[~is_mask] = final.predict_proba(X[~is_mask])[:, 1]
    lo, hi = np.nanquantile(oof, args.share), np.nanquantile(oof, 1 - args.share)
    print(f"Пороги по 2023–2024: шорт при p ≤ {lo:.4f}, лонг при p ≥ {hi:.4f}", flush=True)
    mkt = ml_market(base, data, prob, lo, hi)
    (is_s, is_e), (oos_s, oos_e) = bt.PERIODS["is"], bt.PERIODS["oos"]

    cfg = bt.h1()
    variants = [
        ("только LightGBM, как в отборе", cfg),
        ("+ проскальзывание 0,03% на сторону", replace(cfg, risk=replace(cfg.risk, commission_pct=0.08))),
        ("+ вход на час позже", replace(cfg, entry_on_close=True)),
        ("+ свободные деньги в LQDT", replace(cfg, cash_yield=True)),
        ("только лонги", replace(cfg, allow_short=False)),
        ("только шорты", replace(cfg, allow_long=False)),
    ]
    print(bt.H1_HEADER)
    trades_all = None
    for label, c in variants:
        a = bt.summarize("", *bt.simulate(mkt, is_s, is_e, c), c.capital)
        tr_b, eq_b = bt.simulate(mkt, oos_s, oos_e, c)
        b = bt.summarize("", tr_b, eq_b, c.capital)
        print(bt.fmt_pair(label, a, b), flush=True)
        if trades_all is None:
            trades_all = bt.simulate(mkt, is_s, oos_e, c)[0]
    print(bt.fmt_pair("случайный вход, та же частота",
                      bt.random_baseline(mkt, is_s, is_e, cfg, 10), bt.random_baseline(mkt, oos_s, oos_e, cfg, 10)))

    print("\nПо годам и сторонам (основной вариант, весь период):")
    print(side_year_table(trades_all))
    tr = pd.DataFrame(trades_all)
    by_ticker = tr.groupby("ticker").pnl_abs.agg(["count", "sum"]).sort_values("sum")
    print("\nПо бумагам, ₽:", by_ticker["sum"].round(0).astype(int).to_dict())
    print(f"Бумаг в плюсе: {(by_ticker['sum'] > 0).sum()} из {len(by_ticker)}")


if __name__ == "__main__":
    main()
