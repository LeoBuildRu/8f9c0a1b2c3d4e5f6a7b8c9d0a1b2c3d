"""Обучение рабочей ML-модели агента на всей истории и сохранение в data/models/.

Гиперпараметры и признаки — как в проверенной конфигурации (research/price_ml.py,
research/ml_validate.py). Пороги входа — 10% самых уверенных прогнозов: берутся
из прогнозов «вне выборки» за последние 12 месяцев (модель учится на прошлом
и предсказывает следующий кусок), потому что в живой торговле модель тоже
видит только новые данные. Переобучать раз в месяц:

    python backtest.py --strategy agent_h1 --refresh   # свежая история и сигналы
    python research/train_ml.py
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.agent.ml_model import DEFAULT_MODEL_DIR, FEATURES, MLModel  # noqa: E402
from research.price_ml import build_dataset, models, oof_predict  # noqa: E402


def main() -> None:
    from sklearn.metrics import roc_auc_score
    ap = argparse.ArgumentParser()
    ap.add_argument("--horizon", type=int, default=6)
    ap.add_argument("--share", type=float, default=0.10, help="доля самых уверенных прогнозов в каждую сторону")
    ap.add_argument("--out", default=str(DEFAULT_MODEL_DIR))
    args = ap.parse_args()

    data = build_dataset(args.horizon).sort_values("DATE").reset_index(drop=True)
    X = data[FEATURES].astype(float).fillna(0)
    y = (data.fwd > 0).astype(int).values
    model = models()["LightGBM"]

    oof = oof_predict(model, X, y, data.DATE, folds=8)
    recent = (data.DATE >= data.DATE.max() - pd.Timedelta(days=365)).values & ~np.isnan(oof)
    lo, hi = np.quantile(oof[recent], args.share), np.quantile(oof[recent], 1 - args.share)
    ok = ~np.isnan(oof)
    auc_all, auc_recent = roc_auc_score(y[ok], oof[ok]), roc_auc_score(y[recent], oof[recent])

    final = model.fit(X, y)
    ml = MLModel(final.booster_, list(FEATURES), float(lo), float(hi), {
        "horizon_bars": args.horizon,
        "share": args.share,
        "trained_until": str(data.DATE.max()),
        "rows": int(len(data)),
        "auc_oof_all": round(float(auc_all), 4),
        "auc_oof_last_year": round(float(auc_recent), 4),
        "created_at": datetime.now().isoformat(timespec="seconds"),
    })
    ml.save(Path(args.out))
    print(f"Модель сохранена в {args.out}: {len(data)} строк до {data.DATE.max()}; "
          f"AUC вне выборки {auc_all:.3f} (за год {auc_recent:.3f}); шорт при p ≤ {lo:.4f}, лонг при p ≥ {hi:.4f}")


if __name__ == "__main__":
    main()
