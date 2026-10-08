"""Докачка архива публичного Telegram-канала назад по времени — для бэктеста новостей.

Веб-версия t.me/s/<канал>?before=<id> отдаёт ~20 постов старше указанного.
Идём назад страница за страницей до заданной даты, с паузой между запросами.
Результат дописывается в JSONL; повторный запуск продолжает с самого старого
уже скачанного поста.

    python research/news_backfill.py --channel cbrstocks --until 2022-12-01

Архив можно качать параллельно кусками по номерам постов:
    --before 75000 --stop-id 68000 --out data/news_part2.jsonl
"""

from __future__ import annotations

import argparse
import json
import random
import time
from pathlib import Path
from typing import Dict, List, Optional

import requests
from bs4 import BeautifulSoup

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")


def fetch_page(channel: str, before: Optional[int]) -> Optional[str]:
    url = f"https://t.me/s/{channel}" + (f"?before={before}" if before else "")
    for attempt in range(1, 7):
        try:
            r = requests.get(url, headers={"User-Agent": UA, "Accept-Language": "ru-RU,ru;q=0.9"}, timeout=30)
            if r.status_code == 200 and "tgme_widget_message" in r.text:
                return r.text
            print(f"before={before}: HTTP {r.status_code}, попытка {attempt}", flush=True)
            if r.status_code == 429:
                time.sleep(60 * attempt)
                continue
        except requests.RequestException as exc:
            print(f"before={before}: {type(exc).__name__}, попытка {attempt}", flush=True)
        time.sleep(10 * attempt)
    return None


def parse_posts(html: str) -> List[Dict]:
    soup = BeautifulSoup(html, "html.parser")
    posts = []
    for msg in soup.select(".tgme_widget_message[data-post]"):
        time_el = msg.select_one(".tgme_widget_message_date time")
        text_el = msg.select_one(".tgme_widget_message_text")
        posts.append({
            "id": int(msg["data-post"].rsplit("/", 1)[-1]),
            "date": time_el.get("datetime") if time_el else None,  # ISO, UTC
            "text": text_el.get_text("\n").strip() if text_el else "",
        })
    return posts


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--channel", default="cbrstocks")
    ap.add_argument("--until", default="2022-12-01", help="остановиться на постах старше этой даты")
    ap.add_argument("--out", default="data/news_archive.jsonl")
    ap.add_argument("--before", type=int, help="начать с постов старше этого номера")
    ap.add_argument("--stop-id", type=int, default=0, help="остановиться, дойдя до этого номера")
    args = ap.parse_args()

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    seen = set()
    if out.exists():
        for line in out.read_text(encoding="utf-8").splitlines():
            seen.add(json.loads(line)["id"])
    before = min(seen) if seen else args.before

    pages = 0
    with out.open("a", encoding="utf-8") as f:
        while True:
            html = fetch_page(args.channel, before)
            if html is None:
                print("Страница не загрузилась после повторов — остановка", flush=True)
                break
            posts = parse_posts(html)
            if not posts:
                break
            for p in sorted(posts, key=lambda p: p["id"], reverse=True):
                if p["id"] not in seen:
                    seen.add(p["id"])
                    f.write(json.dumps(p, ensure_ascii=False) + "\n")
            f.flush()
            oldest = min(p["id"] for p in posts)
            oldest_date = min((p["date"] for p in posts if p["date"]), default="")
            pages += 1
            if pages % 25 == 0:
                print(f"страниц {pages}, постов {len(seen)}, дошли до {oldest_date[:10]} (#{oldest})", flush=True)
            if oldest_date and oldest_date < args.until:
                break
            if oldest <= args.stop_id:
                break
            if before is not None and oldest >= before:
                break  # страница не сдвинулась — дальше постов нет
            before = oldest
            time.sleep(1.2 + random.random())
    print(f"Готово: {len(seen)} постов в {out}", flush=True)


if __name__ == "__main__":
    main()
