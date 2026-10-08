"""ML-прогноз направления цены как фильтр сигналов часовой стратегии.

Признаки — те же, по которым решает агент (RSI, MACD, тренды, волатильность,
сила сигнала), плюс недавняя доходность бумаги и IMOEX, объём, час и день
недели; по желанию — новостные признаки из research/news_eval.py.
Цель — знак доходности за следующие N часовых свечей от открытия следующей.

Честность проверки: модели учатся на 2023–2024. Для подбора порога на этом
же периоде используются прогнозы «вне выборки» (обучение на прошлом, прогноз
на следующий кусок). Период 2025–2026 модель не видит до финального прогона.

    python research/price_ml.py --horizon 6
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path
from typing import Dict

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import backtest as bt  # noqa: E402
from app.agent.ml_model import FEATURES, SIGNAL_FEATURES, bar_features  # noqa: E402

SPLIT = pd.Timestamp("2025-01-01")
BASE_FEATURES = SIGNAL_FEATURES


def build_dataset(horizon: int, tickers=None) -> pd.DataFrame:
    """Строки — свечи с сигналом агента; признаки и будущая доходность.
    tickers — свой список бумаг (по умолчанию пул агента); их сигналы должны быть
    в кэше data/signals — его заполняет backtest.py или ml_validate.py."""
    now = pd.Timestamp.now().to_pydatetime()
    imoex = bt.load_history("IMOEX", now, interval=bt.HOURLY).set_index("DATE").sort_index()
    frames = []
    for t in tickers or bt.UNIVERSE:
        cache = bt.SIGNALS_DIR / f"agent_h1_{t}.csv"
        if not cache.exists():
            continue
        sig = pd.read_csv(cache, parse_dates=["DATE"]).set_index("DATE")
        bars = bt.load_history(t, now, interval=bt.HOURLY).set_index("DATE").sort_index()
        f = bar_features(bars, imoex)  # тот же расчёт, что у агента в живой торговле
        # Будущее: вход по открытию следующей свечи, выход по закрытию через horizon свечей
        f["fwd"] = (bars.CLOSE.shift(-horizon) / bars.OPEN.shift(-1) - 1) * 100
        d = sig.join(f, how="inner")
        d["ticker"] = t
        frames.append(d)
    data = pd.concat(frames).reset_index().rename(columns={"index": "DATE"})
    data["hour"] = data.DATE.dt.hour
    data["weekday"] = data.DATE.dt.weekday
    return data.dropna(subset=["fwd"])


def models() -> Dict[str, object]:
    from lightgbm import LGBMClassifier
    from sklearn.ensemble import HistGradientBoostingClassifier, RandomForestClassifier
    from sklearn.linear_model import LogisticRegression
    from sklearn.pipeline import make_pipeline
    from sklearn.preprocessing import StandardScaler
    return {
        "логистическая регрессия": make_pipeline(StandardScaler(), LogisticRegression(C=0.1, max_iter=2000)),
        "случайный лес": RandomForestClassifier(n_estimators=300, max_depth=6, min_samples_leaf=200, n_jobs=3, random_state=0),
        "градиентный бустинг": HistGradientBoostingClassifier(max_depth=4, learning_rate=0.05, max_iter=300,
                                                             min_samples_leaf=200, random_state=0),
        "LightGBM": LGBMClassifier(n_estimators=400, learning_rate=0.03, num_leaves=15, min_child_samples=200,
                                   subsample=0.8, subsample_freq=1, colsample_bytree=0.8, n_jobs=3, verbose=-1,
                                   random_state=0),
    }


def oof_predict(model, X: pd.DataFrame, y: np.ndarray, dates: pd.Series, folds: int = 4) -> np.ndarray:
    """Прогнозы на 2023–2024 «вне выборки»: учимся на прошлом, предсказываем следующий кусок."""
    from sklearn.base import clone
    pred = np.full(len(X), np.nan)
    edges = pd.date_range(dates.min(), dates.max(), periods=folds + 2)
    for k in range(1, folds + 1):
        train = (dates < edges[k]).values
        test = ((dates >= edges[k]) & (dates < edges[k + 1])).values
        if train.sum() < 1000 or test.sum() == 0:
            continue
        pred[test] = clone(model).fit(X[train], y[train]).predict_proba(X[test])[:, 1]
    return pred


def filtered_market(base: bt.Market, data: pd.DataFrame, prob: pd.Series, threshold: float) -> bt.Market:
    """Сигналы агента, с которыми согласна модель: лонг при p ≥ порога, шорт при p ≤ 1 − порога."""
    p = dict(zip(zip(data.DATE, data.ticker), prob))
    buy_i, sell_i = bt.SIG_COLS.index("buy"), bt.SIG_COLS.index("sell")
    day_sigs = {}
    for d, row in base.day_sigs.items():
        new = {}
        for t, v in row.items():
            v = list(v)
            pt = p.get((d, t))
            if pt is None or np.isnan(pt):
                v[buy_i] = v[sell_i] = False
            else:
                v[buy_i] = bool(v[buy_i]) and pt >= threshold
                v[sell_i] = bool(v[sell_i]) and pt <= 1 - threshold
            new[t] = tuple(v)
        day_sigs[d] = new
    return bt.Market(base.day_bars, day_sigs, base.imoex, base.lqdt, intraday=True)


def ml_market(base: bt.Market, data: pd.DataFrame, prob: pd.Series, lo: float, hi: float) -> bt.Market:
    """Сделки только по прогнозу модели: лонг при p ≥ hi, шорт при p ≤ lo. Сигналы агента не нужны."""
    p = dict(zip(zip(data.DATE, data.ticker), prob))
    cols = {c: bt.SIG_COLS.index(c) for c in ("priority", "buy", "sell", "exit_long", "exit_short")}
    day_sigs = {}
    for d, row in base.day_sigs.items():
        new = {}
        for t, v in row.items():
            v = list(v)
            pt = p.get((d, t))
            ok = pt is not None and not np.isnan(pt)
            v[cols["buy"]] = ok and pt >= hi
            v[cols["sell"]] = ok and pt <= lo
            v[cols["priority"]] = abs(pt - 0.5) if ok else 0.0
            v[cols["exit_long"]] = v[cols["exit_short"]] = False
            new[t] = tuple(v)
        day_sigs[d] = new
    return bt.Market(base.day_bars, day_sigs, base.imoex, base.lqdt, intraday=True)


def decile_report(fwd: np.ndarray, prob: np.ndarray) -> str:
    """Средняя будущая доходность по децилям прогноза: есть ли запас над комиссией 0,1%."""
    q = pd.qcut(prob, 10, labels=False, duplicates="drop")
    means = pd.Series(fwd).groupby(q).mean()
    return (f"нижний дециль {means.iloc[0]:+.3f}%, верхний {means.iloc[-1]:+.3f}%, "
            f"разница {means.iloc[-1] - means.iloc[0]:+.3f}% (комиссия на круг 0,1%)")


def main() -> None:
    from sklearn.metrics import roc_auc_score
    ap = argparse.ArgumentParser()
    ap.add_argument("--horizon", type=int, default=6, help="горизонт прогноза в часовых свечах")
    ap.add_argument("--news", help="pkl с новостными признаками по (DATE, ticker) из news_eval.py")
    ap.add_argument("--models", default="логистическая регрессия,LightGBM", help="какие модели сравнивать")
    args = ap.parse_args()

    data = build_dataset(args.horizon)
    features = list(FEATURES)
    if args.news:
        news = pd.read_pickle(args.news)
        data = data.merge(news, on=["DATE", "ticker"], how="left")
        news_cols = [c for c in news.columns if c not in ("DATE", "ticker")]
        data[news_cols] = data[news_cols].fillna(0)
        features += news_cols
    X = data[features].astype(float).fillna(0)
    y = (data.fwd > 0).astype(int).values
    is_mask = (data.DATE < SPLIT).values
    print(f"Строк: {len(data)} (2023–2024: {is_mask.sum()}, 2025–2026: {(~is_mask).sum()}); "
          f"доля роста: {y.mean():.3f}; горизонт {args.horizon} ч", flush=True)

    # Опорная торговля без фильтра — тем же движком
    history = {t: bt.load_history(t, pd.Timestamp.now().to_pydatetime(), interval=bt.HOURLY) for t in bt.UNIVERSE}
    imoex_d = bt.load_history("IMOEX", pd.Timestamp.now().to_pydatetime()).set_index("DATE")["CLOSE"]
    lqdt = bt.load_lqdt(pd.Timestamp.now().to_pydatetime())
    base_mkt = bt.build_markets(["agent_h1"], history, imoex_d, lqdt, bt.PERIODS["all"][0], 1, False, True)["agent_h1"]
    cfg = bt.h1()
    (is_s, is_e), (oos_s, oos_e) = bt.PERIODS["is"], bt.PERIODS["oos"]
    print(bt.H1_HEADER)
    print(bt.fmt_pair("агент без ML-фильтра",
                      bt.summarize("", *bt.simulate(base_mkt, is_s, is_e, cfg), cfg.capital),
                      bt.summarize("", *bt.simulate(base_mkt, oos_s, oos_e, cfg), cfg.capital)), flush=True)

    wanted = args.models.split(",")
    for name, model in models().items():
        if name not in wanted:
            continue
        t0 = time.time()
        oof = oof_predict(model, X[is_mask], y[is_mask], data.DATE[is_mask])
        final = model.fit(X[is_mask], y[is_mask])
        oos_p = final.predict_proba(X[~is_mask])[:, 1]
        prob = pd.Series(np.nan, index=data.index)
        prob[is_mask] = oof
        prob[~is_mask] = oos_p
        ok = ~np.isnan(oof)
        auc_is = roc_auc_score(y[is_mask][ok], oof[ok])
        auc_oos = roc_auc_score(y[~is_mask], oos_p)
        # Порог выбираем только по 2023–2024 (прогнозы вне выборки)
        best = None
        for thr in (0.50, 0.52, 0.54, 0.56):
            r = bt.summarize("", *bt.simulate(filtered_market(base_mkt, data, prob, thr), is_s, is_e, cfg), cfg.capital)
            if r["trades"] >= 300 and (best is None or r["pf"] > best[1]["pf"]):
                best = (thr, r)
        thr, r_is = best
        r_oos = bt.summarize("", *bt.simulate(filtered_market(base_mkt, data, prob, thr), oos_s, oos_e, cfg), cfg.capital)
        print(f"{name}: AUC 2023–2024 {auc_is:.3f}, 2025–2026 {auc_oos:.3f}; порог {thr:.2f}; {time.time() - t0:.0f} с",
              flush=True)
        print(f"  2025–2026: {decile_report(data.fwd.values[~is_mask], oos_p)}", flush=True)
        print(bt.fmt_pair(f"  агент + {name}", r_is, r_oos), flush=True)
        # Только ML: самые уверенные прогнозы; пороги — квантили прогнозов 2023–2024
        for share in (0.05, 0.10):
            lo, hi = np.nanquantile(oof, share), np.nanquantile(oof, 1 - share)
            m = ml_market(base_mkt, data, prob, lo, hi)
            print(bt.fmt_pair(f"  только {name}, {int(share * 100)}% крайних",
                              bt.summarize("", *bt.simulate(m, is_s, is_e, cfg), cfg.capital),
                              bt.summarize("", *bt.simulate(m, oos_s, oos_e, cfg), cfg.capital)), flush=True)


if __name__ == "__main__":
    main()
