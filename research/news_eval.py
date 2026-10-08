"""Оценка методов анализа новостей на реальной реакции рынка.

1. Привязка: к каким бумагам относится пост. Канал сам ставит хэштеги:
   #SBER — компания, #Нефтянка — отрасль, #Макро/#Политика — весь рынок.
2. Реакция: как двигалась цена после публикации — от открытия первой часовой
   свечи, начавшейся после поста (раньше войти нельзя), за 1 ч, 4 ч и ~сутки.
   Для бумаг берётся движение сверх индекса, для рыночных новостей — сам IMOEX.
3. Методы оценивают новость числом от −1 (плохо для бумаги) до +1 (хорошо).
   Сравниваем на 2025–2026; обучаемые методы видят только 2023–2024.

Запуск (на сервере, тяжёлый):
    python research/news_eval.py --methods lexicon,fin_tiny,sent_tiny,sent_base,zeroshot,e5_lr
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
from datetime import timedelta
from pathlib import Path
from typing import Callable, Dict, List

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.agent.news_analyzer import NewsAnalyzer  # noqa: E402
from app.agent.trader_agent import PRIORITY_TICKERS  # noqa: E402

DATA = ROOT / "data"
OUT = DATA / "news_eval"
MSK = timedelta(hours=3)
SPLIT = pd.Timestamp("2025-01-01")
HORIZONS = {"1ч": 1, "4ч": 4, "сутки": 14}  # в часовых свечах (основная + вечерняя сессия)

# ── Данные ──────────────────────────────────────────────────────────────────
def load_posts() -> pd.DataFrame:
    rows = {}
    for path in sorted(DATA.glob("news_*.jsonl")):
        for line in path.read_text(encoding="utf-8").splitlines():
            p = json.loads(line)
            if p.get("text") and p.get("date"):
                rows[p["id"]] = p
    df = pd.DataFrame(rows.values())
    df["date"] = pd.to_datetime(df["date"], utc=True).dt.tz_convert(None)
    return df.sort_values("date").reset_index(drop=True)


def targets_for(text: str) -> List[tuple]:
    """(бумага, уровень) — та же привязка, что у агента, но только бумаги пула и рынок."""
    return [(t, scope) for t, scope in NewsAnalyzer.targets(text) if t in PRIORITY_TICKERS or scope == "market"]


def load_bars() -> Dict[str, pd.DataFrame]:
    from backtest import HOURLY, load_history  # история кэшируется в data/history
    bars = {}
    for t in PRIORITY_TICKERS + ["IMOEX"]:
        df = load_history(t, pd.Timestamp.now().to_pydatetime(), interval=HOURLY)
        bars[t] = df.set_index("DATE").sort_index()
    return bars


def reactions(pairs: pd.DataFrame, bars: Dict[str, pd.DataFrame]) -> pd.DataFrame:
    """Доходность от первой цены после поста; для бумаг — сверх IMOEX."""
    idx = bars["IMOEX"]
    out = {f"r_{h}": [] for h in HORIZONS}
    for ticker, published in zip(pairs.ticker, pairs.date):
        b = bars[ticker]
        t_msk = published + MSK
        i = b.index.searchsorted(t_msk)  # первая свеча, начавшаяся после поста
        ok = i < len(b) and (b.index[i] - t_msk) < pd.Timedelta(days=4)
        for name, n in HORIZONS.items():
            if not ok or i + n - 1 >= len(b):
                out[f"r_{name}"].append(np.nan)
                continue
            start, end = b.index[i], b.index[i + n - 1]
            r = b.CLOSE.iloc[i + n - 1] / b.OPEN.iloc[i] - 1
            if ticker != "IMOEX":
                j0, j1 = idx.index.searchsorted(start), idx.index.searchsorted(end, side="right") - 1
                if 0 <= j0 <= j1 < len(idx):
                    r -= idx.CLOSE.iloc[j1] / idx.OPEN.iloc[j0] - 1
            out[f"r_{name}"].append(r * 100)
    return pairs.assign(**out)


# ── Методы ──────────────────────────────────────────────────────────────────
def clean(text: str) -> str:
    text = re.sub(r"#\S+", " ", text)
    return re.sub(r"\s+", " ", text).strip()[:1000]


def m_lexicon(texts: List[str]) -> np.ndarray:
    return np.array([NewsAnalyzer._score_text(t)[0] for t in texts])


def _classifier(model: str) -> Callable[[List[str]], np.ndarray]:
    def run(texts: List[str]) -> np.ndarray:
        from transformers import pipeline
        clf = pipeline("text-classification", model=model, top_k=None, truncation=True, max_length=256, device=-1)
        id2label = clf.model.config.id2label
        print(f"  {model}: метки {id2label}", flush=True)
        scores = []
        for out in clf([clean(t) for t in texts], batch_size=32):
            d = {o["label"].lower(): o["score"] for o in out}
            pos = sum(v for k, v in d.items() if "pos" in k)
            neg = sum(v for k, v in d.items() if "neg" in k)
            scores.append(pos - neg)
        return np.array(scores)
    return run


def m_zeroshot(texts: List[str]) -> np.ndarray:
    from transformers import pipeline
    clf = pipeline("zero-shot-classification", model="MoritzLaurer/mDeBERTa-v3-base-xnli-multilingual-nli-2mil7", device=-1)
    labels = ["хорошая новость для акций компании", "плохая новость для акций компании", "нейтральная новость"]
    scores = []
    for i in range(0, len(texts), 16):
        for out in clf([clean(t) for t in texts[i:i + 16]], candidate_labels=labels, multi_label=False):
            d = dict(zip(out["labels"], out["scores"]))
            scores.append(d[labels[0]] - d[labels[1]])
    return np.array(scores)


METHODS = {
    "lexicon": m_lexicon,
    "fin_tiny": _classifier("mxlcw/rubert-tiny2-russian-financial-sentiment"),
    "sent_tiny": _classifier("seara/rubert-tiny2-russian-sentiment"),
    "sent_base": _classifier("blanchefort/rubert-base-cased-sentiment"),
    "zeroshot": m_zeroshot,
}


def e5_embeddings(texts: List[str]) -> np.ndarray:
    from sentence_transformers import SentenceTransformer
    model = SentenceTransformer("intfloat/multilingual-e5-small", device="cpu")
    return model.encode(["query: " + clean(t) for t in texts], batch_size=64, normalize_embeddings=True,
                        show_progress_bar=False)


def m_e5_lr(pairs: pd.DataFrame, emb: Dict[int, np.ndarray], target: str) -> np.ndarray:
    """Логистическая регрессия на эмбеддингах: учится предсказывать знак реакции на 2023–2024.

    Для 2023–2024 отдаются прогнозы «вне выборки» (обучение на более ранних
    месяцах), чтобы эту оценку можно было честно использовать как признак."""
    from sklearn.base import clone
    from sklearn.linear_model import LogisticRegression
    scope = pd.get_dummies(pairs.scope).reindex(columns=["company", "sector", "market"], fill_value=0).values
    X = np.hstack([np.vstack([emb[i] for i in pairs.id]), scope])
    y = pairs[target].values
    usable = ~np.isnan(y) & (y != 0)
    is_mask = (pairs.date < SPLIT).values
    model = LogisticRegression(C=0.5, max_iter=2000)
    out = np.full(len(pairs), np.nan)
    edges = pd.date_range(pairs.date[is_mask].min(), SPLIT, periods=6)
    for k in range(1, 5):
        train = usable & (pairs.date < edges[k]).values
        test = ((pairs.date >= edges[k]) & (pairs.date < edges[k + 1])).values
        if train.sum() > 500 and test.any():
            out[test] = clone(model).fit(X[train], y[train] > 0).predict_proba(X[test])[:, 1]
    final = clone(model).fit(X[usable & is_mask], y[usable & is_mask] > 0)
    out[~is_mask] = final.predict_proba(X[~is_mask])[:, 1]
    return out * 2 - 1


# ── Отчёт ───────────────────────────────────────────────────────────────────
def evaluate(pairs: pd.DataFrame, score_col: str, ret_col: str, threshold: float) -> dict:
    d = pairs[~pairs[ret_col].isna()]
    sig = d[d[score_col].abs() >= threshold]
    signed = np.sign(sig[score_col]) * sig[ret_col]
    rho = d[[score_col, ret_col]].corr(method="spearman").iloc[0, 1] if len(d) > 10 else np.nan
    return {
        "пар": len(d),
        "сигналов": len(sig),
        "покрытие%": round(len(sig) / max(len(d), 1) * 100, 1),
        "угадано%": round((signed > 0).mean() * 100, 1) if len(sig) else np.nan,
        "доход_бп": round(signed.mean() * 100, 1) if len(sig) else np.nan,  # базисные пункты
        "t": round(signed.mean() / (signed.std() / np.sqrt(len(sig))), 2) if len(sig) > 2 else np.nan,
        "spearman": round(rho, 3),
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--methods", default="lexicon,fin_tiny,sent_tiny,sent_base,zeroshot,e5_lr")
    ap.add_argument("--zeroshot-limit", type=int, default=6000, help="zero-shot медленный: столько постов с компаниями")
    args = ap.parse_args()
    OUT.mkdir(parents=True, exist_ok=True)

    posts = load_posts()
    print(f"Постов: {len(posts)}, {posts.date.min():%Y-%m-%d} — {posts.date.max():%Y-%m-%d}", flush=True)
    rows = [(r.id, r.date, r.text, t, scope) for r in posts.itertuples() for t, scope in targets_for(r.text)]
    pairs = pd.DataFrame(rows, columns=["id", "date", "text", "ticker", "scope"])
    covered = pairs.id.nunique()
    print(f"Привязано к бумагам или рынку: {covered} постов из {len(posts)} ({covered / len(posts) * 100:.0f}%); "
          f"пар пост-бумага: {len(pairs)} ({pairs.scope.value_counts().to_dict()})", flush=True)
    old = sum(1 for t in posts.text if NewsAnalyzer._alias_mentions(t))
    print(f"Для сравнения: поиск по названиям в тексте (как было) — {old} постов ({old / len(posts) * 100:.0f}%)",
          flush=True)

    pairs = reactions(pairs, load_bars())
    print(f"Реакция посчитана: {pairs.r_1ч.notna().sum()} пар", flush=True)

    texts = posts.set_index("id").text
    methods = args.methods.split(",")
    for name in methods:
        cache = OUT / f"{name}.pkl"
        if name == "e5_lr":
            # Кэш эмбеддингов дополняется: считаем только посты, которых в нём нет
            emb_cache = OUT / "e5_emb.pkl"
            emb = pd.read_pickle(emb_cache) if emb_cache.exists() else {}
            missing = [i for i in pairs.id.unique() if i not in emb]
            if missing:
                t0 = time.time()
                emb.update(zip(missing, e5_embeddings(list(texts.loc[missing].values))))
                pd.to_pickle(emb, emb_cache)
                print(f"  эмбеддинги e5: {len(missing)} постов за {time.time() - t0:.0f} с", flush=True)
            for h in HORIZONS:
                pairs[f"e5_lr_{h}"] = m_e5_lr(pairs, emb, f"r_{h}")
            continue
        if cache.exists():
            scores = pd.read_pickle(cache)
            missing = [i for i in texts.index if i not in scores.index] if name != "zeroshot" else []
            if missing:  # новые посты после прошлого прогона
                scores = pd.concat([scores, pd.Series(METHODS[name](list(texts.loc[missing].values)), index=missing)])
                pd.to_pickle(scores, cache)
        else:
            ids = texts.index
            if name == "zeroshot":  # только посты про компании и отрасли, свежие — на них проверка
                ids = pairs[(pairs.scope != "market") & (pairs.date >= SPLIT)].id.unique()[: args.zeroshot_limit]
            t0 = time.time()
            scores = pd.Series(METHODS[name](list(texts.loc[ids].values)), index=ids)
            pd.to_pickle(scores, cache)
            print(f"  {name}: {len(ids)} постов за {time.time() - t0:.0f} с", flush=True)
        pairs[name] = pairs.id.map(scores)

    # Оценки языковых моделей считаются отдельно (research/news_llm.py) на выборке постов
    for path in sorted(OUT.glob("llm_*.pkl")):
        pairs[path.stem] = pairs.id.map(pd.read_pickle(path))
        methods.append(path.stem)

    pairs.drop(columns=["text"]).to_pickle(OUT / "pairs.pkl")
    oos = pairs[pairs.date >= SPLIT]
    is_part = pairs[pairs.date < SPLIT]
    print("\n=== Проверка на 2025–2026: насколько оценка метода совпадает с реакцией рынка")
    print("сигнал — 20% самых уверенных оценок метода (порог по 2023–2024); угадано% — доля сигналов,")
    print("где знак оценки совпал со знаком движения; доход_бп — средний ход в сторону оценки в")
    print("базисных пунктах (0,01%), до комиссий; t > 2 — уже не похоже на случайность\n")
    for h in HORIZONS:
        rows = []
        for name in methods:
            col = f"e5_lr_{h}" if name == "e5_lr" else name
            if col not in oos:
                continue
            for scope in ["company", "sector", "market"]:
                part = oos[oos.scope == scope]
                if part[col].notna().sum() == 0:
                    continue
                train_scores = is_part.loc[is_part.scope == scope, col].abs().dropna()
                nonzero = train_scores[train_scores > 0]
                # Порог — 20% самых уверенных оценок на 2023–2024; для ЯМ (только 2025–2026) — |оценка| ≥ 0,5
                thr = max(float(nonzero.quantile(0.8)), 1e-9) if len(nonzero) else 0.25
                rows.append({"метод": name, "уровень": scope, **evaluate(part.dropna(subset=[col]), col, f"r_{h}", thr)})
        print(f"--- горизонт {h}")
        print(pd.DataFrame(rows).to_string(index=False), flush=True)


if __name__ == "__main__":
    main()
