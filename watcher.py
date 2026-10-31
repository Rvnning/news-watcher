#!/usr/bin/env python3
"""Token-free news watcher: polls RSS/JSON sources, dedupes, scores with rules.
No LLM calls. No outbound messages unless WEBHOOK_URL is set (it is not)."""
import gzip, json, os, re, sqlite3, sys, time, fcntl, traceback, html
import urllib.request, urllib.parse, urllib.error
import xml.etree.ElementTree as ET
from email.utils import parsedate_to_datetime
from datetime import datetime, timezone, timedelta

BASE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(BASE, "data")
DB_PATH = os.path.join(DATA, "watcher.db")
CAND = os.path.join(DATA, "candidates.jsonl")
BREAK_LOG = os.environ.get("BREAK_LOG") or os.path.join(DATA, "breaking.log")
RUN_LOG = os.path.join(DATA, "run.log")
KST = timezone(timedelta(hours=9))
CFG = json.load(open(os.path.join(BASE, "config.json"), encoding="utf-8"))


def now():
    return datetime.now(timezone.utc)


def log(msg):
    line = f"{datetime.now(KST).strftime('%Y-%m-%d %H:%M:%S KST')} {msg}"
    print(line, flush=True)
    with open(RUN_LOG, "a", encoding="utf-8") as f:
        f.write(line + "\n")


# ---------------------------------------------------------------- HTTP
def fetch(url):
    req = urllib.request.Request(url, headers={
        "User-Agent": CFG["user_agent"], "Accept-Encoding": "gzip",
        "Accept": "application/rss+xml, application/xml, application/json, text/xml, */*"})
    with urllib.request.urlopen(req, timeout=CFG["request_timeout_sec"]) as r:
        body = r.read(5_000_000)
        if r.headers.get("Content-Encoding") == "gzip":
            body = gzip.decompress(body)
        return body


# ---------------------------------------------------------------- text utils
HANGUL = re.compile(r"[\uac00-\ud7a3]")
TRACK_PARAMS = re.compile(r"^(utm_|fbclid|gclid|ref$|ref_|cmpid|ocid|mod$|source$|from$|rss)", re.I)


def norm_url(u):
    try:
        p = urllib.parse.urlsplit(u.strip())
        q = [(k, v) for k, v in urllib.parse.parse_qsl(p.query) if not TRACK_PARAMS.match(k)]
        host = p.netloc.lower().removeprefix("www.").removeprefix("m.")
        path = p.path.rstrip("/")
        return urllib.parse.urlunsplit(("https", host, path, urllib.parse.urlencode(sorted(q)), ""))
    except Exception:
        return u.strip()


def clean_title(t):
    t = html.unescape(re.sub(r"<[^>]+>", "", t or "")).strip()
    return re.sub(r"\s+", " ", t)


STOP = set("the a an of to in on for and or is are was with by at from as its it that this be after over new says said".split())


def title_tokens(title):
    t = re.sub(r"\[[^\]]*\]|\([^)]*\)", " ", title.lower())
    t = re.sub(r"[^\w\s]", " ", t)
    if len(HANGUL.findall(t)) >= 4:  # Korean: character bigrams (robust to particles)
        s = re.sub(r"\s+", "", t)
        return "ko", {s[i:i + 2] for i in range(len(s) - 1)}
    return "en", {w for w in t.split() if len(w) > 1 and w not in STOP}


def jaccard(a, b):
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


_kw_cache = {}


def kw_regex(kw):
    if kw not in _kw_cache:
        if HANGUL.search(kw) or not re.match(r"^[\w&\- .]+$", kw):
            _kw_cache[kw] = re.compile(re.escape(kw))
        else:
            flags = 0 if (kw.isupper() and len(kw) <= 5) else re.I
            _kw_cache[kw] = re.compile(r"(?<![A-Za-z0-9])" + re.escape(kw) + r"(?![A-Za-z0-9])", flags)
    return _kw_cache[kw]


def kw_score(text, kws):
    hits, s = [], 0.0
    for kw, w in kws.items():
        if kw_regex(kw).search(text):
            hits.append(kw); s += w
    return s, hits


def parse_date(s):
    if not s:
        return None
    s = s.strip()
    try:
        d = parsedate_to_datetime(s)
    except Exception:
        try:
            d = datetime.fromisoformat(s.replace("Z", "+00:00"))
        except Exception:
            return None
    if d.tzinfo is None:
        d = d.replace(tzinfo=KST)
    return d.astimezone(timezone.utc)


# ---------------------------------------------------------------- parsers
def local(tag):
    return tag.rsplit("}", 1)[-1].lower()


def parse_feed(body, src):
    root = ET.fromstring(body)
    out = []
    for el in root.iter():
        if local(el.tag) not in ("item", "entry"):
            continue
        d = {}
        for ch in el:
            n = local(ch.tag)
            if n == "link":
                href = ch.attrib.get("href")
                if href and ch.attrib.get("rel", "alternate") == "alternate":
                    d.setdefault("link", href)
                elif ch.text and ch.text.strip():
                    d.setdefault("link", ch.text.strip())
            elif n in ("title", "pubdate", "published", "updated", "date"):
                d.setdefault(n, (ch.text or "").strip())
            elif n == "source":
                d["publisher"] = (ch.text or "").strip()
        title = clean_title(d.get("title", ""))
        link = d.get("link", "")
        if not title or not link:
            continue
        publisher = d.get("publisher")
        if src["name"].startswith("GoogleNews") and " - " in title:
            title, pub2 = title.rsplit(" - ", 1)
            publisher = publisher or pub2
        pub = parse_date(d.get("pubdate") or d.get("published") or d.get("updated") or d.get("date"))
        out.append(dict(title=title, url=link, published=pub, publisher=publisher or src["name"], engagement=None))
    return out


def parse_hn(body, src):
    out = []
    for h in json.loads(body).get("hits", []):
        created = datetime.fromtimestamp(h["created_at_i"], timezone.utc)
        url = h.get("url") or f"https://news.ycombinator.com/item?id={h['objectID']}"
        out.append(dict(title=h.get("title", ""), url=url, published=created, publisher="HackerNews",
                        engagement={"points": h.get("points") or 0, "comments": h.get("num_comments") or 0,
                                    "hn": f"https://news.ycombinator.com/item?id={h['objectID']}"}))
    return out


def parse_reddit(body, src):
    out = []
    for c in json.loads(body).get("data", {}).get("children", []):
        d = c.get("data", {})
        if d.get("stickied"):
            continue
        out.append(dict(title=d.get("title", ""), url=d.get("url") or "https://www.reddit.com" + d.get("permalink", ""),
                        published=datetime.fromtimestamp(d.get("created_utc", 0), timezone.utc), publisher=src["name"],
                        engagement={"points": d.get("ups", 0), "comments": d.get("num_comments", 0)}))
    return out


def fetch_binance(src):
    cc = CFG["crypto_price"]
    out, last_err, got = [], None, 0
    for sym, label in cc["symbols"].items():
        kl = None
        for ep in cc["endpoints"]:
            try:
                kl = json.loads(fetch(f"{ep}/api/v3/klines?symbol={sym}&interval=5m&limit=13"))
                break
            except Exception as e:
                last_err = e
        if not kl:
            continue
        got += 1
        o, c = float(kl[0][1]), float(kl[-1][4])
        pct = (c / o - 1) * 100
        log(f"  binance {label}: {c:g} USDT, ~1h change {pct:+.2f}%")
        if abs(pct) < cc["batch_move_pct"]:
            continue
        direction = "급등" if pct > 0 else "급락"
        bucket = now().strftime("%Y%m%d%H")
        out.append(dict(title=f"{label} 1시간 {pct:+.1f}% {direction} ({c:g} USDT)",
                        url=f"signal://binance/{label}/{'up' if pct > 0 else 'down'}/{bucket}",
                        published=now(), publisher="Binance", engagement={"move_pct": round(pct, 2)}))
        time.sleep(0.3)
    if got == 0 and last_err:
        raise last_err
    return out


# ---------------------------------------------------------------- DB
def db():
    con = sqlite3.connect(DB_PATH)
    con.execute("""CREATE TABLE IF NOT EXISTS items(
        id INTEGER PRIMARY KEY, url_norm TEXT UNIQUE, url TEXT, title TEXT, lang TEXT, tokens TEXT,
        source TEXT, publisher TEXT, topic TEXT, published TEXT, first_seen TEXT, last_update TEXT,
        engagement TEXT, cluster_sources TEXT, score REAL, reason TEXT, queue TEXT, fired INTEGER DEFAULT 0)""")
    con.execute("CREATE INDEX IF NOT EXISTS ix_seen ON items(first_seen)")
    con.execute("CREATE TABLE IF NOT EXISTS aliases(url_norm TEXT PRIMARY KEY, item_id INTEGER)")
    con.execute("CREATE TABLE IF NOT EXISTS source_state(name TEXT PRIMARY KEY, fails INTEGER, skip_until_cycle INTEGER, last_ok TEXT, last_err TEXT)")
    con.execute("CREATE TABLE IF NOT EXISTS meta(k TEXT PRIMARY KEY, v TEXT)")
    return con


# ---------------------------------------------------------------- scoring
def classify(title, fixed_topic):
    scores = {t: kw_score(title, v["keywords"]) for t, v in CFG["topics"].items()}
    if fixed_topic != "auto":
        return fixed_topic, scores[fixed_topic]
    best = max(scores, key=lambda t: scores[t][0])
    if scores[best][0] >= CFG["topic_min_keyword_score"]:
        return best, scores[best]
    return "other", (0.0, [])


def score_item(row, src_weight, kw, eng, cluster_n):
    reasons = [f"src{src_weight:g}"]
    s = src_weight
    k = min(kw[0], 5.0)
    if k:
        s += k; reasons.append(f"kw+{k:g}({','.join(kw[1][:5])})")
    bk = max([w for kwd, w in CFG["breaking_keywords"].items() if kw_regex(kwd).search(row["title"])] or [0])
    if bk:
        s += bk; reasons.append(f"breakingkw+{bk:g}")
    pub = datetime.fromisoformat(row["published"])
    age_h = max((now() - pub).total_seconds() / 3600, 0.25)
    if eng:
        if "move_pct" in eng:
            m = abs(eng["move_pct"])
            b = 6.0 if m >= CFG["crypto_price"]["breaking_move_pct"] else 2.0
            s += b; reasons.append(f"price{eng['move_pct']:+.1f}%+{b:g}")
        else:
            pph = eng.get("points", 0) / max(age_h, 0.5)
            cph = eng.get("comments", 0) / max(age_h, 0.5)
            div = 25.0 if row["source"].startswith("HackerNews") else 500.0
            vb = round(min(6.0, pph / div) + min(2.0, cph / div), 2)
            if vb >= 0.1:
                s += vb; reasons.append(f"velocity+{vb:g}({pph:.0f}pts/h)")
    if cluster_n > 1:
        cb = min(6.0, 1.5 * (cluster_n - 1))
        s += cb; reasons.append(f"cluster{cluster_n}+{cb:g}")
    if age_h <= 1:
        s += 1; reasons.append("fresh+1")
    elif age_h > 6:
        s -= 2; reasons.append("old-2")
    if row["topic"] == "other":
        s -= 3; reasons.append("offtopic-3")
    return round(s, 2), " ".join(reasons), age_h


# ---------------------------------------------------------------- hooks / outputs
def fire_webhook(item):
    """Wake a bot for a breaking item. No-op unless WEBHOOK_URL is set."""
    url = os.environ.get("WEBHOOK_URL")
    if not url:
        return False
    try:
        data = json.dumps(item, ensure_ascii=False).encode()
        req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
        urllib.request.urlopen(req, timeout=10).read()
        return True
    except Exception as e:
        log(f"webhook error: {e}")
        return False


def append_jsonl(rec):
    with open(CAND, "a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")


def item_record(con, iid, event):
    r = con.execute("SELECT id,title,url,source,publisher,topic,published,first_seen,engagement,cluster_sources,score,reason,queue FROM items WHERE id=?", (iid,)).fetchone()
    keys = "id title url source publisher topic published first_seen engagement cluster_sources score reason queue".split()
    d = dict(zip(keys, r))
    d["engagement"] = json.loads(d["engagement"]) if d["engagement"] else None
    d["cluster_sources"] = json.loads(d["cluster_sources"])
    d["event"] = event
    d["logged_at"] = now().isoformat()
    return d


def maybe_fire(con, iid, age_h):
    r = con.execute("SELECT score,topic,fired FROM items WHERE id=?", (iid,)).fetchone()
    score, topic, fired = r
    is_break = score >= CFG["breaking_threshold"] and age_h <= CFG["breaking_max_age_hours"] and topic != "other"
    if is_break and not fired:
        con.execute("UPDATE items SET queue='breaking', fired=1 WHERE id=?", (iid,))
        rec = item_record(con, iid, "breaking")
        sent = fire_webhook(rec)
        with open(BREAK_LOG, "a", encoding="utf-8") as f:
            f.write(json.dumps({"ts": datetime.now(KST).isoformat(timespec="seconds"), "topic": rec["topic"],
                                "title": rec["title"], "url": rec["url"], "score": rec["score"],
                                "reason": rec["reason"], "webhook_sent": sent}, ensure_ascii=False) + "\n")
        return True
    return False


# ---------------------------------------------------------------- main cycle
def process(con, src, items, recent, stats):
    max_age = timedelta(hours=CFG["max_item_age_hours"])
    win = timedelta(hours=CFG["cluster_window_hours"])
    for it in items:
        title = clean_title(it["title"])
        if not title:
            continue
        pub = it["published"] or now()
        if pub > now() + timedelta(minutes=10):
            pub = now()
        if now() - pub > max_age:
            stats["too_old"] += 1
            continue
        un = it["url"] if it["url"].startswith("signal://") else norm_url(it["url"])
        lang, toks = title_tokens(title)
        topic, kw = classify(title, src["topic"])
        if src["type"] == "hn" and topic == "other":
            topic, kw = "ai_tech", (0.0, [])
        ts = now().isoformat()
        ex = con.execute("SELECT item_id FROM aliases WHERE url_norm=?", (un,)).fetchone()
        target = ex[0] if ex else None
        if target is None:
            thr = CFG["title_similarity"]["bigram_jaccard" if lang == "ko" else "word_jaccard"]
            best, bj = None, 0.0
            for rid, rlang, rtoks, rfs in recent:
                if rlang == lang and now() - rfs <= win:
                    j = jaccard(toks, rtoks)
                    if j > bj:
                        best, bj = rid, j
            if best is not None and bj >= thr:
                target = best
                con.execute("INSERT OR IGNORE INTO aliases VALUES(?,?)", (un, target))
        if target is not None:
            # existing story: merge source into cluster, refresh engagement, rescore
            row = con.execute("SELECT title,source,topic,published,engagement,cluster_sources FROM items WHERE id=?", (target,)).fetchone()
            cs = set(json.loads(row[5]))
            key = it["publisher"] or src["name"]
            changed = key not in cs
            cs.add(key)
            eng = json.loads(row[4]) if row[4] else None
            if it["engagement"] and row[1] == src["name"]:
                eng = it["engagement"]; changed = True
            if not changed:
                stats["dup"] += 1
                continue
            stats["merged"] += 1
            r = dict(title=row[0], source=row[1], topic=row[2], published=row[3])
            ftopic = CFG["topics"].get(row[2])
            kw2 = kw_score(row[0], ftopic["keywords"]) if ftopic else (0.0, [])
            w = next((s["weight"] for s in CFG["sources"] if s["name"] == row[1]), 1.0)
            sc, reason, age_h = score_item(r, w, kw2, eng, len(cs))
            con.execute("UPDATE items SET cluster_sources=?, engagement=?, score=?, reason=?, last_update=? WHERE id=?",
                        (json.dumps(sorted(cs), ensure_ascii=False), json.dumps(eng) if eng else None, sc, reason, ts, target))
            if maybe_fire(con, target, age_h):
                stats["breaking"] += 1
                append_jsonl(item_record(con, target, "upgraded_breaking"))
            continue
        # new story
        r = dict(title=title, source=src["name"], topic=topic, published=pub.isoformat())
        cs = [it["publisher"] or src["name"]]
        sc, reason, age_h = score_item(r, src["weight"], kw, it["engagement"], 1)
        cur = con.execute("""INSERT INTO items(url_norm,url,title,lang,tokens,source,publisher,topic,published,first_seen,last_update,
                     engagement,cluster_sources,score,reason,queue) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,'batch')""",
                          (un, it["url"], title, lang, json.dumps(sorted(toks), ensure_ascii=False), src["name"], it["publisher"],
                           topic, pub.isoformat(), ts, ts, json.dumps(it["engagement"]) if it["engagement"] else None,
                           json.dumps(cs, ensure_ascii=False), sc, reason))
        iid = cur.lastrowid
        con.execute("INSERT OR IGNORE INTO aliases VALUES(?,?)", (un, iid))
        recent.append((iid, lang, toks, now()))
        stats["new"] += 1
        fired = maybe_fire(con, iid, age_h)
        if fired:
            stats["breaking"] += 1
        append_jsonl(item_record(con, iid, "new"))


def run_cycle():
    con = db()
    cyc = int((con.execute("SELECT v FROM meta WHERE k='cycle'").fetchone() or ["0"])[0]) + 1
    con.execute("INSERT OR REPLACE INTO meta VALUES('cycle',?)", (str(cyc),))
    con.commit()
    log(f"=== cycle {cyc} start ===")
    since = (now() - timedelta(hours=CFG["cluster_window_hours"])).isoformat()
    recent = [(r[0], r[1], set(json.loads(r[2])), datetime.fromisoformat(r[3]))
              for r in con.execute("SELECT id,lang,tokens,first_seen FROM items WHERE first_seen>=?", (since,))]
    total = {"new": 0, "merged": 0, "dup": 0, "too_old": 0, "breaking": 0}
    ok, failed = [], []
    for src in CFG["sources"]:
        st = con.execute("SELECT fails, skip_until_cycle FROM source_state WHERE name=?", (src["name"],)).fetchone()
        if st and st[1] and cyc < st[1]:
            log(f"  skip {src['name']} (backoff until cycle {st[1]})")
            continue
        stats = {"new": 0, "merged": 0, "dup": 0, "too_old": 0, "breaking": 0}
        try:
            if src["type"] == "binance":
                items = fetch_binance(src)
            else:
                body = fetch(src["url"])
                items = {"rss": parse_feed, "hn": parse_hn, "reddit": parse_reddit}[src["type"]](body, src)
                if not items:
                    raise ValueError("0 items parsed")
            process(con, src, items, recent, stats)
            con.execute("INSERT OR REPLACE INTO source_state VALUES(?,?,?,?,?)", (src["name"], 0, 0, now().isoformat(), None))
            con.commit()
            ok.append(src["name"])
            log(f"  ok {src['name']}: fetched={len(items)} {stats}")
        except Exception as e:
            con.rollback()
            fails = (st[0] if st else 0) + 1
            skip = cyc + min(CFG["source_failure_backoff_cycles"] * fails, 24) if fails >= 2 else 0
            err = f"{type(e).__name__}: {str(e)[:150]}"
            con.execute("INSERT OR REPLACE INTO source_state VALUES(?,?,?,?,?)",
                        (src["name"], fails, skip, None, err))
            con.commit()
            failed.append(src["name"])
            log(f"  FAIL {src['name']}: {err}")
        for k in total:
            total[k] += stats[k]
        time.sleep(CFG["delay_between_requests_sec"])
    log(f"=== cycle {cyc} done: ok={len(ok)} failed={len(failed)} {total} failed_sources={failed} ===")
    con.close()


if __name__ == "__main__":
    os.makedirs(DATA, exist_ok=True)
    lockf = open(os.path.join(DATA, "cycle.lock"), "w")
    try:
        fcntl.flock(lockf, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        log("another cycle is running; exiting")
        sys.exit(0)
    try:
        run_cycle()
    except Exception:
        log("cycle crashed: " + traceback.format_exc())
        sys.exit(1)
