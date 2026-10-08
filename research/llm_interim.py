"""Промежуточное сравнение оценок языковой модели с другими методами на тех же постах."""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from research.news_eval import OUT  # noqa: E402

pairs = pd.read_pickle(OUT / "pairs.pkl")
for path in sorted(OUT.glob("llm_*.pkl")):
    llm = pd.read_pickle(path)
    d = pairs[pairs.id.isin(llm.index)].assign(llm=lambda x: x.id.map(llm))
    print(f"{path.stem}: {llm.size} постов, распределение оценок {llm.value_counts().sort_index().to_dict()}")
    for h in ("r_1ч", "r_4ч", "r_сутки"):
        rows = []
        for method in ("llm", "sent_tiny", "lexicon"):
            for scope in ("company", "sector", "market"):
                part = d[(d.scope == scope) & d[h].notna() & d[method].notna()]
                sig = part[part[method].abs() >= (0.5 if method == "llm" else 0.3)]
                s = np.sign(sig[method]) * sig[h]
                rows.append({"метод": method, "уровень": scope, "сигналов": len(sig),
                             "угадано%": round((s > 0).mean() * 100, 1) if len(sig) else np.nan,
                             "доход_бп": round(s.mean() * 100, 1) if len(sig) else np.nan,
                             "t": round(s.mean() / (s.std() / np.sqrt(len(sig))), 2) if len(sig) > 2 else np.nan})
        print(f"--- {h}")
        print(pd.DataFrame(rows).to_string(index=False))
