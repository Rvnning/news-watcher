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
# Longest first so "에서" is not left behind after a shorter particle.
_PARTICLES = ("으로부터", "에서부터", "이라고", "이라는", "까지", "부터", "으로", "에서", "에게", "한테",
              "이라", "라는", "은", "는", "이", "가", "을", "를", "의", "에", "와", "과", "도", "만", "로", "께", "랑")
# 2-char keywords may continue a compound ("국회의원", "한미일") but not a different word.
_PREFIX_BLOCK = {"유가": ("족", "증권"), "장애": ("인",), "상장": ("학",), "금리": ("단",)}
_NOISE_RE = [re.compile(p, re.I) for p in CFG.get("filters", {}).get("noise_patterns", [])]


def kw_regex(kw):
    if kw not in _kw_cache:
        if HANGUL.search(kw) or not re.match(r"^[\w&\- .]+$", kw):
            _kw_cache[kw] = re.compile(re.escape(kw))
        else:
            flags = 0 if (kw.isupper() and len(kw) <= 5) else re.I
            _kw_cache[kw] = re.compile(r"(?<![A-Za-z0-9])" + re.escape(kw) + r"(?![A-Za-z0-9])", flags)
    return _kw_cache[kw]


# "이유가" ends with the letters 유가 but is not oil. "고유가"/"국제유가" are.
_SUFFIX_PRE_BLOCK = {"유가": {"이"}}


def _strip_hangul_edges(token):
    return re.sub(r"^[^\uac00-\ud7a3]+|[^\uac00-\ud7a3]+$", "", token)


def _prefix_ok(kw, pre):
    if not pre or len(kw) >= 3:
        return True
    return pre not in _SUFFIX_PRE_BLOCK.get(kw, ())


def _rest_ok(kw, rest):
    if rest == "":
        return True
    stripped = rest
    changed = True
    while changed and stripped:
        changed = False
        for p in _PARTICLES:
            if stripped == p or (stripped.endswith(p) and len(stripped) > len(p)):
                stripped = stripped[:-len(p)]
                changed = True
                break
    if stripped == "":
        return True
    if len(kw) >= 3:
        return True
    return len(stripped) <= 2 and not stripped.startswith(_PREFIX_BLOCK.get(kw, ()))


def _match_hangul_token(kw, token):
    if not token:
        return False
    if token == kw:
        return True
    if token.endswith(kw) and _prefix_ok(kw, token[:-len(kw)]):
        return True
    for p in _PARTICLES:  # longest first
        if token.endswith(p) and len(token) - len(p) >= 2:
            return _match_hangul_token(kw, token[:-len(p)])
    if token.startswith(kw) and _rest_ok(kw, token[len(kw):]):
        return True
    return False


def hangul_keyword_in_text(kw, text):
    """Match a Hangul keyword on words, not as a raw substring.

    Substring search treated 유가족 / 이유가 / 액화석유가스 as 유가(oil) and
    장학생 as 상장. A keyword hits as a whole word, a compound suffix
    (국제유가), or a short compound prefix (국회의원, 한미일) — not inside
    an unrelated word.
    """
    spaced = re.sub(r"[^0-9A-Za-z\uac00-\ud7a3]+", " ", text)
    for raw in spaced.split():
        if _match_hangul_token(kw, _strip_hangul_edges(raw)):
            return True
    return False


def keyword_in_text(kw, text):
    if re.fullmatch(r"[\uac00-\ud7a3]+", kw):
        return hangul_keyword_in_text(kw, text)
    return kw_regex(kw).search(text) is not None


def kw_score(text, kws):
    hits = [kw for kw in kws if keyword_in_text(kw, text)]
    # "비트코인" should not also score the shorter "코인"; "OpenAI" should not also score "AI".
    hits = [h for h in hits if not any(h != other and h.lower() in other.lower() for other in hits)]
    return sum(kws[h] for h in hits), hits


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


_OPINION_URL = re.compile(r"/(?:opinion|opinions|column|columns|commentary|editorial)s?(?:/|$)", re.I)


def noise_title(title, url=""):
    if url and _OPINION_URL.search(url):
        return True
    return any(rx.search(title) for rx in _NOISE_RE)


def strong_hits(topic, hits):
    weak = set(CFG.get("filters", {}).get("weak_keywords", {}).get(topic, []))
    return [h for h in hits if h not in weak]


def breaking_in_title(title):
    return any(keyword_in_text(k, title) for k in CFG["breaking_keywords"])


def politics_in_scope(hits, title):
    """한미 정치만 남긴다. 제3국 이름은 한미 닻이 있을 때만 남긴다."""
    foreign = CFG.get("filters", {}).get("foreign_markers", [])
    has_foreign = any(m in title for m in foreign)
    # 헌법재판소·대통령처럼 어느 나라 기사에나 나오는 단어는 제3국을 구하지 못한다.
    kr_us = {"이재명", "트럼프", "Trump", "북한", "김정은", "한미", "국민의힘", "민주당", "더불어민주당",
             "여당", "야당", "백악관", "White House", "Congress", "Senate", "GOP", "Democrats",
             "Republicans", "Vance", "밴스", "중간선거", "midterm", "midterms", "정상회담"}
    if has_foreign:
        if any(h in kr_us for h in hits) or re.search(r"순방|방한|정상회담|이재명|李|여야|국힘|민주", title):
            return True
        return False
    anchors = set(CFG.get("filters", {}).get("politics_anchors", []))
    if any(h in anchors for h in hits):
        return True
    return "대통령" in hits and HANGUL.search(title) is not None


def passes_relevance(topic, kw, title):
    """Fixed-topic feeds used to keep every item. Require a real on-beat keyword."""
    _score, hits = kw
    if topic == "other":
        return False
    if topic == "politics":
        if not hits or not politics_in_scope(hits, title):
            return False
        # A lone "Trump" or "북한" match is how weather, features, and trivia get in.
        # The Korean president's own remarks are the exception: the beat is that office.
        if len(hits) >= 2 or breaking_in_title(title):
            return True
        if any(h in {"탄핵", "impeach", "shutdown", "셧다운", "이재명", "특검", "indict"} for h in hits):
            return True
        return "대통령" in hits and HANGUL.search(title)
    if strong_hits(topic, hits):
        return True
    # [속보] + a weak keyword still keeps a real incident (airport outage, etc.).
    return bool(hits) and breaking_in_title(title)


def _signal_in_title(pattern, title):
    if re.fullmatch(r"[\uac00-\ud7a3]+", pattern):
        return keyword_in_text(pattern, title)
    return re.search(r"(?<![A-Za-z0-9])" + re.escape(pattern) + r"(?:s|es|ers|er|ed|ing)?(?![A-Za-z0-9])",
                      title, re.I) is not None


def salient_tokens(title):
    signals = CFG.get("filters", {}).get("dedup", {}).get("signals", {})
    found = set()
    for token, patterns in signals.items():
        if any(_signal_in_title(p, title) for p in patterns):
            found.add(token)
    # "하락 마감" / "dives" is the same session as "급락" when the index or Intel is named.
    move = bool(re.search(r"하락|급락|폭락|추락|출렁|털썩|사이드카|증발|쇼크|충격|뚝|↓", title)) or bool(
        re.search(r"(?<![A-Za-z])(?:plunge|crash|wipe|worst|slides?|dives?|tumbles?|slumps?|tanks?|drop)(?![A-Za-z])",
                  title, re.I))
    if move and found & {"nasdaq", "intel"}:
        found.add("selloff")
        found.add("ustech")
    elif "selloff" in found and found & {"nasdaq", "intel"}:
        found.add("ustech")
    if move and found & {"kospi", "kosdaq"}:
        found.add("selloff")
        found.add("krindex")
    return found


# Common news words. Sharing these does not mean two headlines are the same event.
_CONTENT_STOP = set("""
첫 점검 챙긴다 강화 나서 찾아 방문 핵심 당부 속도 확보 오늘 내일 이번 관련 위해 대한 통해 따라
지난 올해 내년 지난달 연속 역대 최대 규모 발표 전망 시장 한국 미국 중국 일본 글로벌 세계 국내 해외
정부 국회 대통령 비트코인 암호화폐 가상자산 코인 반도체 증시 코스피 주가 급락 급등 하락 상승 투자
기업 산업 경제 금융 은행 금리 환율 수출 수입 달러 억원 조원 한다 했다 이다 그리고 우리 있는 없는
이번주 다음주 가운데 이후 이전 현재 최근 모두 가장 매우 다시 계속 가능 필요 예정 공식 직접 설명
협력 추진 논의 대응 지원 확대 감소 증가 기록 마감 출발 개장 폐장 장중 시간 오전 오후 회장 사장
트럼프 중간선거 대선 선거 민주당 공화당 백악관 행정부 정상회담 머니투데이 연합뉴스
bitcoin crypto market price stock stocks trump fed election midterms midterm says said after before
about their from with this that have been will into over just more than year years day days week
white house administration president korean korea japan japanese sec court
the and how are for you was its not has can but his who now new one out all may any our per via amid
what does here most when where which while than then them your
삼성 삼성전자 하이닉스 openai 엔비디아 nvidia 애플 apple 구글 google 테슬라 tesla
해킹 탈취 거래소 바이낸스 binance 출금 자금
""".split())
# One-syllable particles eat real nouns (공매도, 머니투데이). Only peel long endings.
_CONTENT_PARTICLE = re.compile(
    r"(으로부터|에서부터|이라고|이라는|까지|부터|으로|에서|에게|한테)$")


def content_tokens(title):
    """Words that can show two outlets rewrote the same event.

    A shared proper name (이재용, FATF) is kept even when the rest of the
    headline was rewritten. Generic market words are not.
    """
    raw = re.sub(r"\[[^\]]*\]|\([^)]*\)", " ", title or "")
    raw = re.sub(r"[^0-9A-Za-z\uac00-\ud7a3\s]", " ", raw)
    out = set()
    for w in raw.split():
        if re.fullmatch(r"[\uac00-\ud7a3]+", w):
            changed = True
            while changed and len(w) > 2:
                changed = False
                m = _CONTENT_PARTICLE.search(w)
                if m and len(w) - len(m.group()) >= 2:
                    w = w[:len(w) - len(m.group())]
                    changed = True
            if len(w) >= 2 and w not in _CONTENT_STOP:
                out.add(w)
        else:
            wl = w.lower()
            if wl in _CONTENT_STOP or len(wl) <= 2 or wl.isdigit():
                continue
            out.add(wl)
    out |= _event_anchors(title)
    return out


def _event_anchors(title):
    """Names for events whose rewrites share almost no ordinary words.

    Each anchor is a specific incident. Two anchors that must stay apart
    (Turkey's visit, Timor's summit) are exclusive in find_cluster.
    """
    t = title or ""
    low = t.lower()
    out = set()
    if "이재용" in t:
        out.add("@leejy")
    if "시정연설" in t:
        out.add("@speech")
    if "fatf" in low or "자금세탁방지기구" in t:
        out.add("@fatf")
    if "키옥시아" in t or "kioxia" in low:
        out.add("@kioxia")
    if "튀르키예" in t or "튀르키에" in t or "에르도안" in t or "訪韓" in t:
        out.add("@turkey")
    if "형제" in t and re.search(r"방한|정상회담|국빈", t):
        out.add("@turkey")
    if "동티모르" in t:
        out.add("@timor")
    if re.search(r"비트코인|bitcoin|사토시|satoshi", t, re.I) and re.search(
            r"(?<![0-9])18\s*주년|(?<![0-9])18\s*년|(?<![0-9])18\s*years|turns\s*18|white\s*paper|whitepaper|백서",
            t, re.I):
        out.add("@btc18")
    if "수출" in t and "반도체" in t and re.search(r"10월|500억|1조|1153|564억|9300억", t):
        out.add("@export")
    if re.search(r"logan|로건|댈러스", low) and re.search(r"rate|금리|hike|인상|올려", low):
        out.add("@fedhike")
    if re.search(r"retail|소매|individual", low) and re.search(r"mining|miner|채굴", low):
        out.add("@retailmine")
    if re.search(r"midterm|중간선거", low) and re.search(r"bitcoin|비트코인|crypto|암호화폐", low) and re.search(
            r"200|1억\s*7600|쏟아", t, re.I):
        out.add("@midbet")
    if re.search(r"openai|오픈\s*ai", low) and re.search(r"funding|유치|모금|조달|목표액", low):
        out.add("@oairound")
    if "공매도" in t and "금감원" in t:
        out.add("@short")
    if "주담대" in t and "신용대출" in t:
        out.add("@loans")
    if re.search(r"국내주식형|머니마켓", t) and "채권" in t and re.search(r"ETF|etf", t):
        out.add("@etfrot")
    if ("시총" in t and "1조" in t) or re.search(r"\$1 trillion|\b1 trillion\b", low):
        out.add("@wipe")
    if re.search(r"비트코인|bitcoin", low) and re.search(r"금|gold", low) and re.search(
            r"추락|동반|worst|2011", low):
        out.add("@goldbtc")
    if re.search(r"\bKT\b|kt클라우드|KT클라우드", t) and "데이터센터" in t:
        out.add("@ktdc")
    if "예금" in t and re.search(r"4\s*%|4%대|연\s*4", t):
        out.add("@dep4")
    if re.search(r"대통령|李", t) and re.search(r"9000억|9천억|1조\s*달러|1조달러|100억불", t) and re.search(
            r"수출|감격|영토|만세", t):
        out.add("@expcheer")
    if re.search(r"링거|코피", t):
        out.add("@nose")
    if re.search(r"xai|스페이스\s*x|spacex", low) and re.search(r"lawsuit|소송|오염|pollution", low):
        out.add("@xailaw")
    if "우주" in t and re.search(r"시 한|시를 썼|poem", t):
        out.add("@poem")
    if "학습" in t and re.search(r"거부|차단|쓰지", t):
        out.add("@optout")
    if re.search(r"국민은행", t) and re.search(r"GPU|gpu|금융주선", t):
        out.add("@kbgpu")
    if re.search(r"10조|10兆", t) and "데이터센터" in t:
        out.add("@jpdc")
    if "유정복" in t:
        out.add("@yoo")
    if "대미투자" in t:
        out.add("@usinv")
    if re.search(r"바이낸스|binance", low) and re.search(
            r"해킹|탈취|털|hack|heist|attack|63억|60억|6 billion|\$6b|8조|8\.7조", low):
        out.add("@bhack")
    if re.search(r"바이낸스|코인거래소", t) and re.search(r"출금 재개|출금 단계|복구|보충|전액", t):
        out.add("@brestore")
    if re.search(r"\bFBI\b", t) and re.search(r"60억|6 billion|\$6B|6B\+", t, re.I):
        out.add("@fbihack")
    if "보안관제" in t or ("해킹" in t and "대응" in t and "당국" in t):
        out.add("@finhack")
    if "[이번주 증시]" in t:
        out.add("@weekstk")
    if "데이터센터" in t and re.search(r"장애 예측|고장 예측|로봇", t):
        out.add("@dcrobot")
    if re.search(r"SNS|sns", t) and re.search(r"손가락|신경전|10건", t):
        out.add("@presns")
    if re.search(r"트럼프|Trump", t) and "대화" in t and re.search(r"북한|北|해킹|소행", t):
        out.add("@trumpnk")
    return out


def _char_trigrams(title):
    s = re.sub(r"[^0-9A-Za-z\uac00-\ud7a3]", "", (title or "").lower())
    if len(s) < 3:
        return {s} if s else set()
    return {s[i:i + 3] for i in range(len(s) - 2)}


def find_cluster(recent, lang, toks, topic, salient, content=None, grams=None):
    """Return an existing item id to merge into, or None.

    Title Jaccard misses paraphrases ('돼지 눈' vs '국민은 전부 돼지'). Those
    share salient entities. A new fact token (보상, 사망, 피소) starts a new row
    so a follow-up is not swallowed by the first headline.
    """
    dedup = CFG.get("filters", {}).get("dedup", {})
    if not dedup:
        return None
    win = timedelta(hours=CFG["cluster_window_hours"])
    t_thr = CFG["title_similarity"]["bigram_jaccard" if lang == "ko" else "word_jaccard"]
    soft = dedup.get("title_jaccard_with_shared", 0.38)
    min_shared = dedup.get("min_shared_salient", 2)
    sal_need = dedup.get("salient_jaccard", 0.5)
    new_facts = set(dedup.get("new_fact_tokens", []))
    # Subset merge must name a real entity. {trump} alone must not swallow every Trump story.
    # People (trump, 대통령) are in many unrelated stories, so they cannot be the reason
    # a narrower headline is swallowed by a broader one.
    subset_anchors = {"binance", "ustech", "nasdaq", "intel", "kospi", "kosdaq",
                      "altman", "samsung", "sk", "openai", "oil", "anthropic",
                      "tether", "coinbase", "upbit", "polymarket", "pig", "multiculture",
                      "basicincome", "tax", "denuclear", "diplomat"}
    content = content or set()
    grams = grams or set()
    # A headline that names both state visits must not pull each visit into one row.
    exclusive = {"@turkey", "@timor"}
    best, bj = None, 0.0
    for rec in recent:
        rid, rlang, rtoks, rfs, rtopic, rsal = rec[:6]
        rcontent = rec[6] if len(rec) > 6 else set()
        rgrams = rec[7] if len(rec) > 7 else set()
        if now() - rfs > win:
            continue
        same_topic = rtopic == topic
        # 보상 / 사망 / 피소 / ETF flows are a different development in either direction.
        # A later hack headline must not fold into the compensation row, and a
        # compensation headline must not fold into the original hack row.
        # 보상·사망·소송은 새 사실이다. 다만 출금 재개처럼 같은 후속 사건의
        # 이름 닻을 양쪽이 달고 있으면 표현만 다른 같은 기사다.
        shared_anchors = {tok for tok in (content & rcontent) if tok.startswith("@")}
        countries = {"@turkey", "@timor"}
        if (content & countries) and (rcontent & countries) and (content & countries) != (rcontent & countries):
            shared_anchors -= countries
        if (salient ^ rsal) & new_facts and not (shared_anchors & {"@brestore"}):
            continue
        # A Trump reaction ("모른다", "주시") is not the hack headline.
        # 이재명 / 대통령 are spelled too many ways to use the same gate.
        if (salient ^ rsal) & {"trump"}:
            continue
        core_new, core_old = salient - new_facts, rsal - new_facts
        ents = subset_anchors | {"btc", "eth", "nk", "krindex"}
        # Binance's compensation and Polymarket's compensation are different incidents.
        if (core_new & ents) and (core_old & ents) and not (core_new & ents & core_old):
            continue
        shared = core_new & core_old
        union = core_new | core_old
        sj = (len(shared) / len(union)) if union else 0.0
        smaller, larger = (core_new, core_old) if len(core_new) <= len(core_old) else (core_old, core_new)
        named_subset = len(smaller) >= 2 and smaller <= larger and bool(smaller & subset_anchors)
        # "A $6.4 billion hack" with no exchange name still belongs to the Binance cluster.
        anon_hack = smaller <= {"hack", "btc", "eth"} and "binance" in larger and "hack" in smaller
        subset_hit = named_subset or anon_hack
        # Two Binance-hack headlines are one incident even when one also names
        # Bitcoin or North Korea and the salient Jaccard falls under 0.5.
        # Altman-Samsung and Altman-SK are each one meeting. Denuclearization
        # headlines from the same day are one announcement.
        same_incident = (
            {"binance", "hack"} <= shared
            or ("altman" in shared and bool(shared & {"samsung", "sk"}))
            or "denuclear" in shared
        )
        # hack/vasset/selloff alone are too common to glue unrelated stories together.
        generic = {"hack", "vasset", "selloff"}
        salient_hit = subset_hit or same_incident or (
            len(shared) >= min_shared and sj >= sal_need and bool(shared - generic)
        )
        j = jaccard(toks, rtoks) if rlang == lang else 0.0
        title_hit = same_topic and rlang == lang and (j >= t_thr or (j >= soft and len(shared) >= 1))
        # Rewrites that share a proper name or enough content words, even when
        # the old title-bigram threshold misses them.
        inter = content & rcontent
        uni = content | rcontent
        cjac = (len(inter) / len(uni)) if uni else 0.0
        cover = (len(inter) / min(len(content), len(rcontent))) if content and rcontent else 0.0
        anchors = {t for t in inter if t.startswith("@")}
        # Turkey's state visit and the Timor summit are different events. A bridge
        # headline that names both must not glue them, even when the wording overlaps.
        exclusive_split = bool(
            (content & exclusive) and (rcontent & exclusive)
            and (content & exclusive) != (rcontent & exclusive))
        if exclusive_split:
            anchors = set()
        gj = jaccard(grams, rgrams)
        companies = {"samsung", "sk", "apple", "intel", "nvidia"}
        company_clash = (salient & companies) and (rsal & companies) and not (salient & companies & rsal)
        content_hit = (not company_clash) and (not exclusive_split) and (
            bool(anchors)
            or (len(inter) >= 2 and (cjac >= 0.34 or cover >= 0.65))
            or (len(inter) >= 1 and gj >= 0.22 and cjac >= 0.2)
        )
        if not same_topic and not content_hit:
            # Same event on two beats: the Nasdaq drop on the tech feed and the markets feed.
            cross = len(shared) >= 3 or bool(shared & {"nasdaq", "intel", "ustech", "krindex", "binance", "altman"})
            if not cross:
                continue
        if salient_hit or title_hit or content_hit:
            # A name-only rewrite can share zero salient tokens. Still keep it:
            # a zero score used to lose to the initial 0 and the row stayed split.
            score = len(shared) + (1.5 if "binance" in rsal and "hack" in salient else 0) + sj
            if content_hit:
                score += 1 + len(anchors) + cjac
            if title_hit:
                score += j
            if score > bj:
                best, bj = rid, score
    return best


def headline_rank(title):
    """Keyword weight plus a breaking marker. Used to keep the clearest headline."""
    best_kw = 0.0
    for topic in CFG["topics"].values():
        best_kw = max(best_kw, kw_score(title, topic["keywords"])[0])
    bk = max([w for kwd, w in CFG["breaking_keywords"].items() if kw_regex(kwd).search(title)] or [0])
    return best_kw + bk


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
    elif age_h > 18:
        s -= 3; reasons.append("old-3")
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


def maybe_fire(con, iid, age_h, kw_points=0.0, engagement=None):
    r = con.execute("SELECT score,topic,fired FROM items WHERE id=?", (iid,)).fetchone()
    score, topic, fired = r
    move = abs((engagement or {}).get("move_pct") or 0)
    price_break = move >= CFG["crypto_price"]["breaking_move_pct"]
    kw_ok = kw_points >= CFG.get("breaking_min_keyword_score", 1.0) or price_break
    is_break = score >= CFG["breaking_threshold"] and age_h <= CFG["breaking_max_age_hours"] and topic != "other" and kw_ok
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
    for it in items:
        title = clean_title(it["title"])
        if not title:
            continue
        if noise_title(title, it.get("url") or ""):
            stats["noise"] += 1
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
        if not passes_relevance(topic, kw, title):
            stats["offtopic"] += 1
            continue
        salient = salient_tokens(title)
        content = content_tokens(title)
        grams = _char_trigrams(title)
        ts = now().isoformat()
        ex = con.execute("SELECT item_id FROM aliases WHERE url_norm=?", (un,)).fetchone()
        target = ex[0] if ex else None
        if target is None:
            target = find_cluster(recent, lang, toks, topic, salient, content, grams)
            if target is not None:
                con.execute("INSERT OR IGNORE INTO aliases VALUES(?,?)", (un, target))
        if target is not None:
            # existing story: merge source into cluster, refresh engagement, rescore.
            # A clearer rewrite (속보, higher keyword weight) replaces the displayed
            # title and url. url_norm stays the first copy so the row identity holds.
            row = con.execute(
                "SELECT title,source,topic,published,engagement,cluster_sources,url FROM items WHERE id=?",
                (target,)).fetchone()
            cs = set(json.loads(row[5]))
            key = it["publisher"] or src["name"]
            changed = key not in cs
            cs.add(key)
            eng = json.loads(row[4]) if row[4] else None
            if it["engagement"] and row[1] == src["name"]:
                eng = it["engagement"]; changed = True
            better = headline_rank(title) > headline_rank(row[0])
            if not changed and not better:
                stats["dup"] += 1
                continue
            stats["merged"] += 1
            display_title = title if better else row[0]
            display_url = it["url"] if better and it.get("url") else row[6]
            r = dict(title=display_title, source=row[1], topic=row[2], published=row[3])
            ftopic = CFG["topics"].get(row[2])
            kw2 = kw_score(display_title, ftopic["keywords"]) if ftopic else (0.0, [])
            w = next((s["weight"] for s in CFG["sources"] if s["name"] == row[1]), 1.0)
            sc, reason, age_h = score_item(r, w, kw2, eng, len(cs))
            if better:
                con.execute("""UPDATE items SET title=?, url=?, lang=?, tokens=?, cluster_sources=?, engagement=?,
                               score=?, reason=?, last_update=? WHERE id=?""",
                            (display_title, display_url, lang, json.dumps(sorted(toks), ensure_ascii=False),
                             json.dumps(sorted(cs), ensure_ascii=False), json.dumps(eng) if eng else None,
                             sc, reason, ts, target))
                for i, rec in enumerate(recent):
                    if rec[0] == target:
                        # Keep the event name even when the clearer headline omits it.
                        # Do not accumulate both state-visit names: a bridge headline
                        # would then pull the other visit into this row.
                        old_c = rec[6] if len(rec) > 6 else set()
                        kept = {tok for tok in old_c if str(tok).startswith("@")}
                        add = {tok for tok in content if str(tok).startswith("@")}
                        countries = {"@turkey", "@timor"}
                        if (kept & countries) and (add & countries) and (kept & countries) != (add & countries):
                            add -= countries
                        if len((kept | add) & countries) > 1:
                            kept -= countries
                            add -= countries
                        words = {tok for tok in content if not str(tok).startswith("@")}
                        recent[i] = (target, lang, toks, rec[3], rec[4], salient, words | kept | add, grams)
                        break
            else:
                con.execute("UPDATE items SET cluster_sources=?, engagement=?, score=?, reason=?, last_update=? WHERE id=?",
                            (json.dumps(sorted(cs), ensure_ascii=False), json.dumps(eng) if eng else None, sc, reason, ts, target))
            if maybe_fire(con, target, age_h, kw2[0], eng):
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
        recent.append((iid, lang, toks, now(), topic, salient, content, grams))
        stats["new"] += 1
        fired = maybe_fire(con, iid, age_h, kw[0], it["engagement"])
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
    recent = []
    for r in con.execute("SELECT id,lang,tokens,first_seen,topic,title FROM items WHERE first_seen>=?", (since,)):
        recent.append((r[0], r[1], set(json.loads(r[2])), datetime.fromisoformat(r[3]), r[4],
                       salient_tokens(r[5]), content_tokens(r[5]), _char_trigrams(r[5])))
    total = {"new": 0, "merged": 0, "dup": 0, "too_old": 0, "breaking": 0, "noise": 0, "offtopic": 0}
    ok, failed = [], []
    for src in CFG["sources"]:
        st = con.execute("SELECT fails, skip_until_cycle FROM source_state WHERE name=?", (src["name"],)).fetchone()
        if st and st[1] and cyc < st[1]:
            log(f"  skip {src['name']} (backoff until cycle {st[1]})")
            continue
        stats = {"new": 0, "merged": 0, "dup": 0, "too_old": 0, "breaking": 0, "noise": 0, "offtopic": 0}
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
