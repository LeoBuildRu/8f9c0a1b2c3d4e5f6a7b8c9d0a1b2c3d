"""Оценка новостей небольшой локальной языковой моделью (Ollama на CPU).

Модель получает пост и бумагу (или «рынок в целом») и отвечает JSON
{"impact": -2..2}: как новость повлияет на цену в ближайшие часы.
Модели медленные, поэтому оценивается случайная выборка постов 2025–2026;
результат сохраняется в data/news_eval/llm_<модель>.pkl и подхватывается
отчётом research/news_eval.py.

    python research/news_llm.py --model qwen2.5:3b --sample 600
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

import pandas as pd
import requests

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from research.news_eval import OUT, SPLIT, clean, load_posts, targets_for  # noqa: E402

OLLAMA = "http://127.0.0.1:11434/api/chat"
SYSTEM = ("Ты аналитик российского фондового рынка. Оцени, как новость повлияет на цену акций "
          "в ближайшие часы после публикации. Отвечай только JSON.")


def ask(model: str, text: str, target: str) -> float:
    prompt = (f"Новость:\n{text}\n\nОцени влияние на: {target}.\n"
              'Ответь JSON вида {"impact": N}, где N — целое от -2 до 2: '
              "-2 сильно негативно, -1 негативно, 0 не влияет или неясно, 1 позитивно, 2 сильно позитивно.")
    body = {
        "model": model,
        "messages": [{"role": "system", "content": SYSTEM}, {"role": "user", "content": prompt}],
        "format": {"type": "object", "properties": {"impact": {"type": "integer", "minimum": -2, "maximum": 2}},
                   "required": ["impact"]},
        "stream": False,
        "think": False,
        "options": {"temperature": 0, "num_ctx": 2048, "num_predict": 20, "num_thread": 3},
    }
    r = requests.post(OLLAMA, json=body, timeout=300)
    r.raise_for_status()
    impact = json.loads(r.json()["message"]["content"])["impact"]
    return max(-2, min(2, int(impact))) / 2


def describe(targets) -> str:
    ticker, scope = targets[0]
    if scope == "market":
        return "российский рынок акций в целом (индекс Мосбиржи)"
    if scope == "sector":
        return f"акции отрасли, в том числе {', '.join(t for t, _ in targets[:4])}"
    return f"акции {ticker}"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", default="qwen2.5:3b")
    ap.add_argument("--sample", type=int, default=600)
    args = ap.parse_args()

    posts = load_posts()
    posts = posts[posts.date >= SPLIT].copy()
    posts["targets"] = posts.text.map(targets_for)
    posts = posts[posts.targets.map(bool)]
    # Поровну постов про компании/отрасли и про рынок — у первых реакция заметнее
    company = posts[posts.targets.map(lambda t: t[0][1] != "market")]
    market = posts[posts.targets.map(lambda t: t[0][1] == "market")]
    sample = pd.concat([company.sample(min(len(company), args.sample * 2 // 3), random_state=0),
                        market.sample(min(len(market), args.sample // 3), random_state=0)])

    out = OUT / f"llm_{args.model.replace(':', '_')}.pkl"
    scores = pd.read_pickle(out) if out.exists() else pd.Series(dtype=float)
    t0, done = time.time(), 0
    for row in sample.itertuples():
        if row.id in scores.index:
            continue
        try:
            scores.loc[row.id] = ask(args.model, clean(row.text), describe(row.targets))
        except Exception as exc:
            print(f"пост {row.id}: {type(exc).__name__}: {exc}", flush=True)
            continue
        done += 1
        if done % 25 == 0:
            pd.to_pickle(scores, out)
            print(f"{done} постов, {(time.time() - t0) / done:.1f} с на пост", flush=True)
    pd.to_pickle(scores, out)
    print(f"Готово: {len(scores)} оценок в {out}", flush=True)


if __name__ == "__main__":
    main()
