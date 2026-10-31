#!/usr/bin/env python3
"""Summary of the candidate queue for a time window.
Usage: python3 report.py [--hours 24] [--since 'YYYY-MM-DD HH:MM'] [--top 10] [--topic crypto] [--queue batch|breaking]
Times are KST (Asia/Seoul)."""
import argparse, json, os, sqlite3
from datetime import datetime, timedelta, timezone

KST = timezone(timedelta(hours=9))
DB = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "watcher.db")
LABELS = {t: v["label"] for t, v in json.load(open(os.path.join(os.path.dirname(DB), "..", "config.json"), encoding="utf-8"))["topics"].items()}
LABELS["other"] = "기타(무관)"

ap = argparse.ArgumentParser()
ap.add_argument("--hours", type=float, default=24)
ap.add_argument("--since", help="KST start, e.g. '2026-10-31 07:00'")
ap.add_argument("--top", type=int, default=10)
ap.add_argument("--topic")
ap.add_argument("--queue", choices=["batch", "breaking"])
ap.add_argument("--include-other", action="store_true", help="include off-topic items in top list")
a = ap.parse_args()

start = (datetime.strptime(a.since, "%Y-%m-%d %H:%M").replace(tzinfo=KST) if a.since
         else datetime.now(KST) - timedelta(hours=a.hours)).astimezone(timezone.utc)
con = sqlite3.connect(DB)
where, args = "first_seen>=?", [start.isoformat()]
if a.topic:
    where += " AND topic=?"; args.append(a.topic)
if a.queue:
    where += " AND queue=?"; args.append(a.queue)

print(f"Window: since {start.astimezone(KST):%Y-%m-%d %H:%M} KST  ->  now {datetime.now(KST):%Y-%m-%d %H:%M} KST")
rows = con.execute(f"SELECT topic, COUNT(*), SUM(queue='breaking'), ROUND(AVG(score),2) FROM items WHERE {where} GROUP BY topic ORDER BY 2 DESC", args).fetchall()
tot = sum(r[1] for r in rows); br = sum(r[2] or 0 for r in rows)
print(f"Total stories: {tot}   breaking: {br}   batch: {tot - br}")
print(f"{'topic':<10}{'label':<12}{'count':>6}{'breaking':>10}{'avg':>7}")
for t, n, b, avg in rows:
    print(f"{t:<10}{LABELS.get(t, t):<12}{n:>6}{b or 0:>10}{avg:>7}")
w2 = where + ("" if a.include_other or a.topic else " AND topic!='other'")
print(f"\nTop {a.top} by score:")
for i, (sc, t, q, title, url, srcs, reason, fs) in enumerate(con.execute(
        f"SELECT score,topic,queue,title,url,cluster_sources,reason,first_seen FROM items WHERE {w2} ORDER BY score DESC LIMIT ?", args + [a.top]), 1):
    fs = datetime.fromisoformat(fs).astimezone(KST).strftime("%m-%d %H:%M")
    print(f"{i:>2}. [{sc:>5}] {t:<8} {'BREAKING' if q == 'breaking' else 'batch':<8} {fs}  {title}")
    print(f"      {url}\n      sources={len(json.loads(srcs))} {reason}")
