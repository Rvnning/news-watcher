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


# A coin line that is only a price print. A hack, a rule change, or an ETF flow stays.
# A spot print ("비트코인 1.05% 오른"), not a dollar size ("Hit $827 million") and not a fee.
_PRICE_TICK = re.compile(
    r"(?:비트코인|이더리움|리플|솔라나|도지코인|Bitcoin|Ethereum|XRP|Solana|BTC|ETH)"
    r".{0,32}\d+(?:\.\d+)?\s*%\s*(?:오른|내린|상승|하락|급등|급락|반등)",
    re.I)
_PRICE_KEEP = re.compile(
    r"해킹|승인|규제|조사|유출|압수|소송|금지|동결|법안|hack|approv|outflow|lawsuit|"
    r"freeze|flaw|resign|scam|exploit|breach|ETF|etf",
    re.I)
# "양산시, …" / "부안군, …" is a local-government blurb, whatever the one keyword was.
# Words that merely end in 시 (증시, 날씨) are not a city.
_LOCAL_GOV = re.compile(r"^(?:\[[^\]]+\]\s*)?([가-힣]{2,4}(?:시|군|구)),")
_LOCAL_GOV_SKIP = ("증시", "날씨", "실시", "당시", "동시", "일시", "주시")


def _local_gov_blurb(title):
    m = _LOCAL_GOV.search(title or "")
    return bool(m) and not m.group(1).endswith(_LOCAL_GOV_SKIP)


def noise_title(title, url=""):
    if url and _OPINION_URL.search(url):
        return True
    if _local_gov_blurb(title):
        return True
    if title and _PRICE_TICK.search(title) and not _PRICE_KEEP.search(title) and not breaking_in_title(title):
        return True
    return any(rx.search(title) for rx in _NOISE_RE)


def strong_hits(topic, hits):
    weak = set(CFG.get("filters", {}).get("weak_keywords", {}).get(topic, []))
    return [h for h in hits if h not in weak]


def breaking_in_title(title):
    return any(keyword_in_text(k, title) for k in CFG["breaking_keywords"])


# A country name sitting in front of 대통령, with no Korea/US anchor, is not this beat.
# The word itself is not listed: 파라과이 and the next country are the same shape.
_KR_US_ANCHOR = re.compile(
    r"이재명|윤석열|트럼프|Trump|북한|한미|국민의힘|국힘|민주|여당|야당|백악관|White House|Congress|"
    r"미국|한국|대한민국|방한|순방|정상회담|李|김정은|특검|국회|검찰|GOP|Democrats|Republicans|"
    r"Senate|중간선거|midterm|White House",
    re.I)
_FOREIGN_PRESIDENT = re.compile(r"([가-힣]{3,8})\s*대통령")
_FOREIGN_PRESIDENT_SKIP = {"우리", "신임", "현직", "전임", "새", "부"}


def foreign_head_of_state(title):
    """True when the president in the headline is a third country's, not Korea or the US."""
    if not title or _KR_US_ANCHOR.search(title):
        return False
    m = _FOREIGN_PRESIDENT.search(title)
    return bool(m) and m.group(1) not in _FOREIGN_PRESIDENT_SKIP


def politics_in_scope(hits, title):
    """한미 정치만 남긴다. 제3국 이름은 한미 닻이 있을 때만 남긴다."""
    if foreign_head_of_state(title):
        return False
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


# Ordinary news words. These are not names, so they cannot glue two headlines together.
_CONTENT_STOP = set("""
첫 점검 챙긴다 강화 나서 찾아 방문 핵심 당부 속도 확보 오늘 내일 이번 관련 위해 대한 통해 따라
지난 올해 내년 지난달 연속 역대 최대 규모 발표 전망 시장 한국 미국 중국 일본 글로벌 세계 국내 해외
정부 국회 대통령 비트코인 암호화폐 가상자산 코인 반도체 증시 코스피 주가 급락 급등 하락 상승 투자
기업 산업 경제 금융 은행 금리 환율 수출 수입 달러 억원 조원 한다 했다 이다 그리고 우리 있는 없는
이번주 다음주 가운데 이후 이전 현재 최근 모두 가장 매우 다시 계속 가능 필요 예정 공식 직접 설명
협력 추진 논의 대응 지원 확대 감소 증가 기록 마감 출발 개장 폐장 장중 시간 오전 오후 회장 사장
중간선거 대선 선거 민주당 공화당 백악관 행정부 정상회담 머니투데이 연합뉴스 데이터센터
해킹 탈취 거래소 출금 자금 기자 뉴스 속보 종합 단독 현장 영상 사진
bitcoin crypto market price stock stocks fed election midterms midterm says said after before
about their from with this that have been will into over just more than year years day days week
white house administration president korean korea japan japanese court
the and how are for you was its not has can but his who now new one out all may any our per via amid
what does here most when where which while than then them your
""".split())
_EN_ENTITY_STOP = {w.lower() for w in """
The A An In On Of To For And Or But At By As Is Are Was With From After Before New How Why What
This That It Its US AI We He She They I If So No Not May Can Could Would Should Will Just More
Over Into After About Says Said After When Where Which While Than Then Them Your Our
""".split()}
# Long particles only. A one-syllable ending is often part of the name itself.
_CONTENT_PARTICLE = re.compile(
    r"(으로부터|에서부터|이라고|이라는|까지|부터|으로|에서|에게|한테)$")
_EN_ENTITY = re.compile(r"[A-Z][A-Za-z0-9]{1,}")


# Case markers that are not part of a name. One-syllable endings such as 도/이
# stay, because they are often the last syllable of the word itself (공매도).
_SHORT_PARTICLE = set("과와은는을를의서")


def _peel_particle(word):
    w = word
    changed = True
    while changed and len(w) > 3:
        changed = False
        m = _CONTENT_PARTICLE.search(w)
        if m and len(w) - len(m.group()) >= 3:
            w = w[:len(w) - len(m.group())]
            changed = True
            continue
        if w[-1] in _SHORT_PARTICLE and len(w) - 1 >= 3:
            w = w[:-1]
            changed = True
    return w


def _title_words(title):
    raw = re.sub(r"\[[^\]]*\]|\([^)]*\)", " ", title or "")
    raw = re.sub(r"[^0-9A-Za-z\uac00-\ud7a3\s]", " ", raw)
    return raw.split()


def content_tokens(title):
    """Content words for title overlap. Names and ordinary words both count here.

    A shared ordinary word is not enough on its own. find_cluster also asks
    for character overlap or a shared proper name.
    """
    out = set()
    for w in _title_words(title):
        if re.fullmatch(r"[\uac00-\ud7a3]+", w):
            w = _peel_particle(w)
            if len(w) >= 2 and w not in _CONTENT_STOP:
                out.add(w)
        else:
            wl = w.lower()
            if wl in _CONTENT_STOP or len(wl) <= 2 or wl.isdigit():
                continue
            out.add(wl)
    return out


def named_entities(title):
    """Proper-name candidates, with no list of people, places, or companies.

    English names are the capitalized tokens in the headline. Hangul has no
    case, so a Hangul token counts only when it is not an ordinary news word
    and is long enough to be a name. Whether it actually identifies one event
    is decided later, by recurrence across outlets inside the time window.
    """
    out = set()
    for m in _EN_ENTITY.finditer(title or ""):
        w = m.group()
        if w.lower() in _EN_ENTITY_STOP or w.lower() in _CONTENT_STOP:
            continue
        if w.isupper() or (len(w) >= 3 and any(ch.islower() for ch in w[1:])):
            out.add(w.lower())
        elif len(w) >= 3:
            out.add(w.lower())
    for w in _title_words(title):
        if not re.fullmatch(r"[\uac00-\ud7a3]+", w):
            continue
        w = _peel_particle(w)
        if 3 <= len(w) <= 8 and w not in _CONTENT_STOP:
            out.add(w)
    return out


def _char_trigrams(title):
    s = re.sub(r"[^0-9A-Za-z\uac00-\ud7a3]", "", (title or "").lower())
    if len(s) < 3:
        return {s} if s else set()
    return {s[i:i + 3] for i in range(len(s) - 2)}


_SPEC_NUM = re.compile(r"\d+\.\d+|\d{4,}")
_QUOTE_SPAN = re.compile(r"[\"“”「『]([^\"“”」』]{4,})[\"“”」』]")
_SESSION_INDEX = (
    ("kospi", re.compile(r"코스피|KOSPI", re.I)),
    ("kosdaq", re.compile(r"코스닥|KOSDAQ", re.I)),
    ("nasdaq", re.compile(r"나스닥|Nasdaq", re.I)),
    ("fx", re.compile(r"원[·/\s]?달러|환율")),
)
_UP_WORD = re.compile(r"상승|급등|오른|반등|오름|강세|전환")
_DOWN_WORD = re.compile(r"하락|급락|내린|약세|밀리|밀려|낙폭|폭락")
_TAPE_WORD = re.compile(r"%|개장|마감|장중|등락|상승|하락|급등|급락|반등|내린|오른|보합")


def specific_numbers(title):
    """Decimals and long figures. Years and round budget totals are not one story.

    7만6000 and 76,000 are the same print, so a 만-grouped figure is expanded first.
    """
    raw = (title or "").replace(",", "").replace("，", "")
    raw = re.sub(r"(\d+)만(\d+)?", lambda m: str(int(m.group(1)) * 10000 + int(m.group(2) or 0)), raw)
    raw = re.sub(r"(?<![A-Za-z0-9])(\d+(?:\.\d+)?)\s*[Kk]\b",
                 lambda m: str(int(float(m.group(1)) * 1000)), raw)
    out = set()
    for m in _SPEC_NUM.finditer(raw):
        s = m.group()
        if re.fullmatch(r"(?:19|20)\d\d", s):
            continue
        out.add(s)
    return out


# A reaction, a column, or a product pitch. These stay in the store but cannot
# reach the score band the scout reads (8) or the breaking line (9).
_SOFT_ROUNDUP = re.compile(r"Tech Today|이\s*시각 주요", re.I)
_SOFT_HARD_ACT = re.compile(
    r"통과|지명|청문|재가|서킷\s*브레이커|사이드\s*카|담보비율|면제|시장안정|"
    r"거래.?중단|매매.?중단|입출금|전면.?중단|서비스.?중단|유출|사임|납입|지지율|"
    r"해킹|hack|breach",
    re.I)
_SOFT_NOISE = re.compile(
    r"답하라|여론전|장외투쟁|셀프면죄|사유화|물구나무|라방|사주|규탄|긴급진단|긴급 설문|리서치센터|"
    r"마켓뷰|마켓인사이드|We Asked|Which Hits|what[- ]if|저항선|지지선|\b2030\b|악수|통장|박람회|의무화|"
    r"간담회|숏\s*베팅|하방\s*위험|급등\s*이유|폭락\s*경고|목표주가|목표가|목표치|지킬까|적금|"
    r"Why\s+\S+(?:\s+\S+){0,6}\s+(?:Surging|Soaring|Rising|Falling)|"
    r"Weekly recap|Extreme Fear|Further Price Drop|추가 하락",
    re.I)
# A demand to undo an event is the reaction, even when the title also says 지명.
_SOFT_DEMAND = re.compile(r"철회하라|맹공|소굴|약탈|전횡|막장|특권")
# A visit, a show, or a first shipment is the schedule, not the decision.
_SOFT_PROMO = re.compile(r"위문|공연|첫\s*수출|첫\s*선적")
# One listed name jumping, not the index. 반도체지수 and 나스닥 stay on the tape.
_INDEX_PRINT = re.compile(
    r"코스피|코스닥|나스닥|Nasdaq|다우|Dow|S&P|비트코인|Bitcoin|BTC|이더리움|Ethereum|"
    r"반도체지수|필라델피아|환율",
    re.I)
_SINGLE_SPIKE = re.compile(r"\d+(?:\.\d+)?\s*%[^%]{0,12}(?:폭등|급등|뛰)")
_RECAP_STAT = re.compile(r"(?:시총|시가총액)")
_COLUMN_BYLINE = re.compile(r"[가-힣]{2,4}의\s*[‘'\"「]")
_TAPE_INTERVIEW = re.compile(r"\bto tape\b", re.I)
_COIN_STACK = re.compile(r"\bbuys?\b", re.I)
# 장외집회 is the rally. 장외투쟁 is the slogan.
_STRESS_REACTION = re.compile(r"답하라|여론전|장외(?!집회)|라방|셀프|사유화|물구나무|사주|비판")
_RALLY = re.compile(r"집회|시위|행진|농성")
_BREAKING_MARK = re.compile(r"\[속보\]|【속보】|\bBREAKING\b|\bJUST IN\b", re.I)


def _single_name_spike(title):
    return bool(_SINGLE_SPIKE.search(title)) and not _INDEX_PRINT.search(title)


def soft_noise(title):
    """True when the headline is commentary, a reaction, or a pitch.

    A roundup, a demand to undo a decision, and a ceremonial visit stay under
    the scout line even if a desk stamps [속보] on them. A rally is the event.
    A bill, a nomination, a trading halt, or a leak keeps the full score
    unless the headline is only demanding that it be undone.
    """
    if not title:
        return False
    if _RALLY.search(title):
        return False
    if _SOFT_ROUNDUP.search(title) or (_RECAP_STAT.search(title) and "증발" in title):
        return True
    if _SOFT_DEMAND.search(title):
        return True
    if _SOFT_PROMO.search(title) and not _SOFT_HARD_ACT.search(title):
        return True
    if _single_name_spike(title):
        return True
    if _TAPE_INTERVIEW.search(title) and re.search(r"interview", title, re.I):
        return True
    if (_COIN_STACK.search(title) and re.search(r"\b(?:Bitcoin|BTC)\b", title)
            and re.search(r"\d{3,}", title) and not _SOFT_HARD_ACT.search(title)):
        return True
    if re.search(r"성공적", title) and re.search(r"수면", title):
        return True
    if _STRESS_REACTION.search(title) and not _SOFT_HARD_ACT.search(title):
        return True
    if _BREAKING_MARK.search(title) or _SOFT_HARD_ACT.search(title):
        return False
    return _SOFT_NOISE.search(title) is not None or _COLUMN_BYLINE.search(title) is not None
_EVENT_VERB = re.compile(r"중단|해킹|유출|사임|납입|청문|재가")


def stress_tag(title):
    """One tag per tape. A reaction is not the tape, and a policy response is its own tape.

    A bill headline that also mentions the opposition protest still belongs to the vote.
    """
    if not title:
        return None
    if re.search(r"지명", title) and re.search(r"장관|청장|수석|총리", title):
        return "nomination"
    if re.search(r"특검", title) and re.search(r"통과|상정|필리버스터|표결|처리", title):
        return "bill"
    if re.search(r"통과", title) and re.search(r"법안|국회|법 ", title):
        return "bill"
    if re.search(r"정상회담", title):
        return "summit"
    if re.search(r"ETF|etf", title) and re.search(r"유출|outflow", title, re.I):
        return "etf-flow"
    if re.search(r"지지율|국정지지", title):
        return "approval"
    if re.search(r"사임|사퇴", title) and re.search(r"대통령", title):
        return "resign"
    if re.search(r"전기요금|전력요금", title) and re.search(r"데이터센터", title):
        return "power"
    if re.search(r"담보비율|반대매매", title):
        return "margin"
    if re.search(r"\bF4\b|시장안정", title):
        return "stabilizer"
    if re.search(r"gpt\s*[- ]?\s*6", title, re.I) and re.search(r"cyber|사이버", title, re.I):
        return "model-cyber"
    if re.search(r"비서실장|chief of staff", title, re.I) and re.search(r"교체|replace", title, re.I):
        return "staff-cos"
    # The closing rebound, not the opening print. 출발/개장 stays its own story.
    if (re.search(r"나스닥|Nasdaq|반도체지수|필라델피아", title, re.I)
            and re.search(r"반등|급등|↑|오른", title)
            and not re.search(r"출발|개장", title)):
        return "us-rebound"
    if _STRESS_REACTION.search(title):
        return None
    # "반도체" alone is not the tape: an oil headline can mention chips in passing.
    if re.search(r"코스피|코스닥|나스닥|삼성전자|SK하이닉스|반도체주", title) and re.search(
            r"서킷\s*브레이커|사이드\s*카|급락|폭락|붕괴|투매|깨져|무너", title):
        return "kr-selloff"
    if re.search(r"반도체\s*(?:급락|폭락|투매|붕괴)", title):
        return "kr-selloff"
    if re.search(r"코스피|코스닥", title) and re.search(
            r"(?:[5-9]|1\d)(?:\.\d+)?\s*%\s*(?:내린|하락|급락|폭락|밀린)", title):
        return "kr-selloff"
    # The same crash, rewritten around the round number instead of the word 급락.
    if re.search(r"코스피|코스닥", title) and re.search(r"5,?000|5천(?:피|선)|오천피", title) and re.search(
            r"붕괴|깨|무너|턱걸이|와르르|검은|급락|폭락|투매", title):
        return "kr-selloff"
    return None


def event_verbs(title):
    return set(_EVENT_VERB.findall(title or ""))


def bill_names(title):
    """The law's own name. 국회 and 통과 are not a name, so two bills stay apart."""
    names = set()
    for m in re.finditer(r"[가-힣]{2,16}법(?:안)?", title or ""):
        w = re.sub(r"안$", "", m.group())
        if len(w) >= 3 and w not in {"법률"}:
            names.add(w)
    return names


def offices(title):
    """부처+직위. 법무장관 and 국세청장 are different posts."""
    found = set()
    for m in re.finditer(r"([가-힣]{2,6})?\s*(장관|청장|수석|총리|실장)", title or ""):
        prefix = re.sub(r"[부의]$", "", m.group(1) or "")
        if len(prefix) >= 2:
            found.add(prefix + m.group(2))
    return found


_APPOINT_SKIP = {"대통령", "이재명", "트럼프", "국민의힘", "민주당", "정부", "국회", "후보", "후보자"}


def appointment_people(title):
    """The nominee, not the president who named them."""
    return {e for e in named_entities(title or "")
            if len(e) >= 3 and e not in _APPOINT_SKIP and not e.startswith("후보")
            and not e.endswith(("장관", "청장", "수석", "총리", "실장", "부"))}


def _quote_jaccard(a, b):
    """Overlap of the quoted clause, so two wires carrying the same sentence meet."""
    ba = "".join(_QUOTE_SPAN.findall(a or ""))
    bb = "".join(_QUOTE_SPAN.findall(b or ""))
    ga, gb = _char_trigrams(ba), _char_trigrams(bb)
    if len(ga) < 4 or len(gb) < 4:
        return 0.0
    return jaccard(ga, gb)


def session_signature(title):
    """Index tape: the index leads the headline, and the print has a direction.

    Open, midday, and close stay apart. Up and down stay apart. A reversal
    (전환) counts as the new direction so the bounce does not join the drop.
    """
    head = re.sub(r"^\[[^\]]+\]\s*", "", title or "")[:24]
    idx = None
    for name, rx in _SESSION_INDEX:
        if rx.search(head):
            idx = name
            break
    # A feature that merely mentions the index ("반등을 기다리기") is not a tape.
    if not idx or not re.search(r"\d+(?:\.\d+)?\s*%|개장|마감|장중|[\d,]{4,}", title or ""):
        return None
    up, down = _UP_WORD.search(title or ""), _DOWN_WORD.search(title or "")
    if re.search(r"전환", title or "") and up:
        direction = "up"
    elif up and down:
        direction = "turn"
    elif up:
        direction = "up"
    elif down:
        direction = "down"
    else:
        direction = "flat"
    if re.search(r"개장|출발", title or ""):
        phase = "open"
    elif re.search(r"마감|종가", title or ""):
        phase = "close"
    elif re.search(r"장중", title or ""):
        phase = "intra"
    else:
        phase = "none"
    return (idx, direction, phase)


def _session_compatible(a, b):
    if not a or not b or a[0] != b[0] or a[1] != b[1] or a[1] == "flat":
        return False
    # The closing print stays its own story. Earlier updates may join each other.
    if "close" in (a[2], b[2]) and a[2] != b[2]:
        return False
    return True


def _merge_session(a, b):
    if not a:
        return b
    if not b or not _session_compatible(a, b):
        return a
    phase = a[2] if a[2] != "none" else b[2]
    return (a[0], a[1], phase)


def find_cluster(recent, lang, toks, topic, salient, content=None, grams=None, entities=None, publisher=None, title=""):
    """Return an existing item id to merge into, or None.

    Title Jaccard misses paraphrases ('돼지 눈' vs '국민은 전부 돼지'). Those
    share salient entities. A new fact token (보상, 사망, 피소) starts a new row
    so a follow-up is not swallowed by the first headline.

    A second path merges outlet rewrites: character trigrams or content-word
    overlap, and a lower bar when the headlines also share a proper name that
    is not already spread across many different stories in the window.
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
    tri_high = dedup.get("char_trigram_jaccard", 0.34)
    tok_high = dedup.get("content_jaccard", 0.40)
    tri_mid = dedup.get("entity_char_trigram", 0.16)
    tok_mid = dedup.get("entity_content_jaccard", 0.22)
    max_df = dedup.get("max_entity_cluster_df", 8)
    # Subset merge must name a real entity. {trump} alone must not swallow every Trump story.
    # People (trump, 대통령) are in many unrelated stories, so they cannot be the reason
    # a narrower headline is swallowed by a broader one.
    subset_anchors = {"binance", "ustech", "nasdaq", "intel", "kospi", "kosdaq",
                      "altman", "samsung", "sk", "openai", "oil", "anthropic",
                      "tether", "coinbase", "upbit", "polymarket", "pig", "multiculture",
                      "basicincome", "tax", "denuclear", "diplomat"}
    content = content or set()
    grams = grams or set()
    entities = entities or set()
    nums = specific_numbers(title)
    sess = session_signature(title)
    ent_df = {}
    for rec in recent:
        if now() - rec[3] > win:
            continue
        for ent in (rec[8] if len(rec) > 8 else ()):
            ent_df[ent] = ent_df.get(ent, 0) + 1
    best, bj = None, 0.0
    for rec in recent:
        rid, rlang, rtoks, rfs, rtopic, rsal = rec[:6]
        rcontent = rec[6] if len(rec) > 6 else set()
        rgrams = rec[7] if len(rec) > 7 else set()
        rentities = rec[8] if len(rec) > 8 else set()
        rpub = rec[9] if len(rec) > 9 else None
        rtitle = rec[10] if len(rec) > 10 else ""
        rnums = rec[11] if len(rec) > 11 else specific_numbers(rtitle)
        rsess = rec[12] if len(rec) > 12 else session_signature(rtitle)
        if now() - rfs > win:
            continue
        # A reaction or a column does not join the event it is talking about,
        # and it cannot become that event's displayed headline.
        if soft_noise(title) != soft_noise(rtitle):
            continue
        same_topic = rtopic == topic
        # 보상 / 사망 / 피소 / ETF flows are a different development in either direction.
        if (salient ^ rsal) & new_facts:
            continue
        # A Trump reaction ("모른다", "주시") is not the hack headline.
        # 이재명 / 대통령 are spelled too many ways to use the same gate.
        if (salient ^ rsal) & {"trump"}:
            continue
        core_new, core_old = salient - new_facts, rsal - new_facts
        ents = subset_anchors | {"btc", "eth", "nk", "krindex"}
        # Binance's compensation and Polymarket's compensation are different incidents.
        # A single selloff tape may name the index in one headline and a chipmaker in another.
        tag_early, rtag_early = stress_tag(title), stress_tag(rtitle)
        # Different laws, or a different post and a different person, are not one tape
        # even when both headlines say 국회 통과 or 지명.
        if tag_early == "bill" and rtag_early == "bill":
            left, right = bill_names(title), bill_names(rtitle)
            if left and right and not (left & right):
                continue
        if tag_early == "nomination" and rtag_early == "nomination":
            same_post = bool(offices(title) & offices(rtitle)) or bool(
                appointment_people(title) & appointment_people(rtitle))
            if not same_post:
                continue
        same_tape = bool(tag_early and tag_early == rtag_early and tag_early not in {"summit", "bill", "nomination"})
        if tag_early == "bill" and rtag_early == "bill":
            same_tape = bool(bill_names(title) & bill_names(rtitle))
        if tag_early == "nomination" and rtag_early == "nomination":
            same_tape = bool(offices(title) & offices(rtitle)) or bool(
                appointment_people(title) & appointment_people(rtitle))
        if (core_new & ents) and (core_old & ents) and not (core_new & ents & core_old) and not same_tape:
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
        inter = content & rcontent
        uni = content | rcontent
        cjac = (len(inter) / len(uni)) if uni else 0.0
        gj = jaccard(grams, rgrams)
        shared_names = entities & rentities
        # A name already sitting on many separate stories is not one event.
        distinctive = {e for e in shared_names if ent_df.get(e, 0) <= max_df}
        cross_outlet = bool(publisher and rpub and publisher != rpub)
        high = gj >= tri_high or cjac >= tok_high
        mid = gj >= tri_mid or (cjac >= tok_mid and len(inter) >= 2)
        # Disjoint names are different events unless the wording itself matches.
        disjoint = bool(entities and rentities and not shared_names)
        entity_hit = bool(distinctive) and mid and cross_outlet and not disjoint
        content_hit = (high or entity_hit) and not (disjoint and not high)
        # A shared print (1.62, 36.9, 5,637) or the same quoted sentence, or the
        # same index tape in the same direction. Opposite sessions do not glue.
        sessions_clash = bool(sess and rsess and not _session_compatible(sess, rsess))
        number_hit = bool(nums and rnums and (nums & rnums)) and same_topic and not sessions_clash
        session_hit = _session_compatible(sess, rsess)
        quote_hit = _quote_jaccard(title, rtitle) >= dedup.get("quote_jaccard", 0.34)
        tag, rtag = stress_tag(title), stress_tag(rtitle)
        stress_hit = bool(tag and tag == rtag and tag not in {"summit", "bill", "nomination"})
        if tag == "summit" and rtag == "summit":
            # Same meeting only when the place or guest is shared. The office title is not a place.
            stress_hit = bool(entities & rentities)
        if tag == "bill" and rtag == "bill":
            stress_hit = bool(bill_names(title) & bill_names(rtitle))
        if tag == "nomination" and rtag == "nomination":
            stress_hit = bool(offices(title) & offices(rtitle)) or bool(
                appointment_people(title) & appointment_people(rtitle))
        # A repeated halt, leak, resignation, or payment can name the company in
        # every headline, so the name is no longer "rare". The shared verb keeps
        # it to one event. Ordinary words never become names.
        verb_hit = bool(event_verbs(title) & event_verbs(rtitle)) and any(len(e) >= 3 for e in shared_names)
        if not same_topic and not content_hit and not session_hit and not quote_hit and not stress_hit and not verb_hit:
            # Same event on two beats: the Nasdaq drop on the tech feed and the markets feed.
            cross = len(shared) >= 3 or bool(shared & {"nasdaq", "intel", "ustech", "krindex", "binance", "altman"})
            if not cross:
                continue
        if salient_hit or title_hit or content_hit or number_hit or session_hit or quote_hit or stress_hit or verb_hit:
            score = len(shared) + (1.5 if "binance" in rsal and "hack" in salient else 0) + sj
            if content_hit or number_hit or session_hit or quote_hit or stress_hit or verb_hit:
                score += 1 + len(distinctive) + max(gj, cjac)
            if number_hit or session_hit or quote_hit or stress_hit or verb_hit:
                score += 1
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
    # Commentary, party reactions, and product pitches stay stored, under the scout line.
    if soft_noise(row.get("title") or ""):
        s = min(s, 7.4); reasons.append("softcap")
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
        entities = named_entities(title)
        publisher = it.get("publisher") or src["name"]
        ts = now().isoformat()
        ex = con.execute("SELECT item_id FROM aliases WHERE url_norm=?", (un,)).fetchone()
        target = ex[0] if ex else None
        if target is None:
            target = find_cluster(recent, lang, toks, topic, salient, content, grams, entities, publisher, title)
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
                        # Narrow to names both headlines use, so a roundup that adds
                        # a second event cannot become the hub for that event.
                        old_ent = rec[8] if len(rec) > 8 else set()
                        shared_ent = (old_ent & entities) if old_ent and entities else (old_ent or entities)
                        old_nums = rec[11] if len(rec) > 11 else set()
                        new_nums = specific_numbers(title)
                        # The displayed headline may be the first copy that actually prints the figure.
                        kept_nums = (old_nums & new_nums) or new_nums or old_nums
                        kept_sess = _merge_session(rec[12] if len(rec) > 12 else None, session_signature(title))
                        recent[i] = (target, lang, toks, rec[3], rec[4], salient, content, grams,
                                     shared_ent, rec[9] if len(rec) > 9 else publisher,
                                     title, kept_nums, kept_sess)
                        break
            else:
                con.execute("UPDATE items SET cluster_sources=?, engagement=?, score=?, reason=?, last_update=? WHERE id=?",
                            (json.dumps(sorted(cs), ensure_ascii=False), json.dumps(eng) if eng else None, sc, reason, ts, target))
                for i, rec in enumerate(recent):
                    if rec[0] == target:
                        old_ent = rec[8] if len(rec) > 8 else set()
                        shared_ent = (old_ent & entities) if old_ent and entities else (old_ent or entities)
                        old_nums = rec[11] if len(rec) > 11 else set()
                        kept_nums = (old_nums & specific_numbers(title)) if old_nums else specific_numbers(title)
                        kept_sess = _merge_session(rec[12] if len(rec) > 12 else None, session_signature(title))
                        kept_title = rec[10] if len(rec) > 10 else title
                        recent[i] = (*rec[:8], shared_ent, rec[9] if len(rec) > 9 else publisher,
                                     kept_title, kept_nums, kept_sess)
                        break
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
        recent.append((iid, lang, toks, now(), topic, salient, content, grams, entities, publisher,
                        title, specific_numbers(title), session_signature(title)))
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
    for r in con.execute(
            "SELECT id,lang,tokens,first_seen,topic,title,publisher FROM items WHERE first_seen>=?", (since,)):
        recent.append((r[0], r[1], set(json.loads(r[2])), datetime.fromisoformat(r[3]), r[4],
                       salient_tokens(r[5]), content_tokens(r[5]), _char_trigrams(r[5]),
                       named_entities(r[5]), r[6] or "",
                       r[5], specific_numbers(r[5]), session_signature(r[5])))
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
