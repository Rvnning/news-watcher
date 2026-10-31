#!/usr/bin/env python3
"""GitHub Actions state helper (stdlib only).
  python3 state.py load  : state/state.jsonl -> data/watcher.db
  python3 state.py dump  : prune data/watcher.db, write state/state.jsonl, trim state/breaking.log
Text JSONL (sorted, one row per line) keeps git diffs/history small vs. a binary SQLite file."""
import json, os, sqlite3, sys
from datetime import datetime, timedelta, timezone

BASE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE)
DATA, STATE = os.path.join(BASE, "data"), os.path.join(BASE, "state")
SJ, BL = os.path.join(STATE, "state.jsonl"), os.path.join(STATE, "breaking.log")
ITEM_DAYS, LOG_DAYS = 3, 7
TABLES = {"items": "id", "aliases": "url_norm", "source_state": "name", "meta": "k"}

def load():
    os.makedirs(DATA, exist_ok=True)
    import watcher
    con = watcher.db()
    if os.path.exists(SJ):
        for line in open(SJ, encoding="utf-8"):
            r = json.loads(line); t = r.pop("_t")
            cols = list(r)
            con.execute(f"INSERT OR REPLACE INTO {t}({','.join(cols)}) VALUES({','.join('?'*len(cols))})", [r[c] for c in cols])
    con.commit(); con.close()

def dump():
    con = sqlite3.connect(os.path.join(DATA, "watcher.db"))
    cut = (datetime.now(timezone.utc) - timedelta(days=ITEM_DAYS)).isoformat()
    con.execute("DELETE FROM items WHERE first_seen < ?", (cut,))
    con.execute("DELETE FROM aliases WHERE item_id NOT IN (SELECT id FROM items)")
    con.commit()
    os.makedirs(STATE, exist_ok=True)
    with open(SJ + ".tmp", "w", encoding="utf-8") as f:
        for t, k in TABLES.items():
            cur = con.execute(f"SELECT * FROM {t} ORDER BY {k}")
            cols = [d[0] for d in cur.description]
            for row in cur:
                d = {"_t": t, **dict(zip(cols, row))}
                f.write(json.dumps(d, ensure_ascii=False, sort_keys=True) + "\n")
    os.replace(SJ + ".tmp", SJ)
    if os.path.exists(BL):
        lcut = datetime.now(timezone.utc) - timedelta(days=LOG_DAYS)
        keep = []
        for line in open(BL, encoding="utf-8"):
            try:
                if datetime.fromisoformat(json.loads(line)["ts"]) >= lcut: keep.append(line)
            except Exception:
                keep.append(line)
        open(BL, "w", encoding="utf-8").writelines(keep)

if __name__ == "__main__":
    {"load": load, "dump": dump}[sys.argv[1]]()
