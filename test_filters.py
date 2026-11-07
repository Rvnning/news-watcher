#!/usr/bin/env python3
"""Regression checks for keyword matching, relevance, and paraphrase dedup."""
import os, sqlite3, tempfile
from datetime import datetime, timedelta, timezone

import watcher

NOW = watcher.now()


def src(topic, name="Test", typ="rss"):
    return {"name": name, "topic": topic, "type": typ, "weight": 2.0}


def item(title, topic_src="markets", hours_ago=1, url=None, publisher=None):
    return dict(title=title, url=url or ("https://example.com/" + str(abs(hash(title)))),
                published=NOW - timedelta(hours=hours_ago), publisher=publisher or "Example",
                engagement=None)


def screen(title, topic):
    s = src(topic)
    pub = NOW - timedelta(hours=1)
    if watcher.noise_title(title):
        return "noise"
    _t, kw = watcher.classify(title, topic)
    if not watcher.passes_relevance(_t, kw, title):
        return "offtopic"
    return None


def test_hangul_false_friends():
    assert not watcher.keyword_in_text("유가", "이태원 참사 유가족 반발")
    assert not watcher.keyword_in_text("유가", "6개월 징역, 이유가")
    assert not watcher.keyword_in_text("유가", "공주 주택서 액화석유가스 폭발")
    assert not watcher.keyword_in_text("유가", "유가증권 시장")
    assert watcher.keyword_in_text("유가", "이란 확전 우려에 국제유가 2%대 급등")
    assert watcher.keyword_in_text("유가", "이란전 고유가에 석유공룡 횡재")
    assert not watcher.keyword_in_text("상장", "고교 상상장학생 모집")
    assert watcher.keyword_in_text("상장", "코스닥 신규 상장")
    assert not watcher.keyword_in_text("장애", "장애인 고용 확대")
    assert watcher.keyword_in_text("장애", "인천공항 네트워크 장애")
    assert watcher.keyword_in_text("대통령", "李대통령은 다문화 정책을 시정")
    assert watcher.keyword_in_text("한미", "한미일 외교장관 회의")
    assert watcher.keyword_in_text("비트코인", "비트코인 급락")
    assert not watcher.keyword_in_text("금리", "금리단길 돼지갈비 맛집")
    assert watcher.keyword_in_text("금리", "기준금리 인상과 금리인하 전망")


def test_relevance_gates():
    assert screen("후티 반군, 아덴 대통령궁·공항 미사일로 타격", "politics") == "offtopic"
    assert screen("[북한날씨] 전 지역 대체로 맑음", "politics") == "noise"
    assert screen("폴란드 헌재소장 직무정지…대통령·총리 권력 다툼", "politics") == "offtopic"
    assert screen("해커, 바이낸스에서 63억 달러 해킹", "crypto") is None
    assert screen("뉴욕증시 기술주 매도…나스닥 2.9% 급락·인텔 29% 폭락", "markets") is None
    assert screen("After Intel’s historic stock drop, analysts say don’t buy the dip", "markets") is None
    assert screen("삼성 사장단, 샘 올트먼 만났다…AI 미래전략 논의", "ai_tech") is None
    assert screen("서울대 AI연구원 제5회 회장배 골프대회", "ai_tech") == "noise"
    assert screen("Here's Why I'm Buying Bitcoin Right Now", "crypto") == "noise"
    assert screen("Crypto exchanges explained | Consumer Reports", "crypto") == "noise"
    assert screen("Serbia just elected the world's brainiest parliament", "ai_tech") == "offtopic"
    assert screen("Nearly 6 million coffee makers recalled over burn risk", "markets") == "offtopic"
    assert screen("이 대통령, 야당 순방 비판에 돼지 눈에는 돼지만 보여", "politics") is None
    assert screen("한미일 외교장관, 북한 비핵화 재확인", "politics") is None
    assert screen("Is Cheesecake Factory decor really so bad?", "politics") == "noise"
    assert screen("[게시판] KT&G장학재단, 상상장학생 모집", "markets") == "noise"
    assert screen("업비트 비트코인 하락…빔 급등, 거래대금 5위", "crypto") == "noise"
    # Third-country politics with no KR/US anchor stays out. A KR state visit does not.
    assert screen("시진핑, 독일·스위스와 연쇄 회담", "politics") == "offtopic"
    assert screen("이재명 대통령, 폴란드 순방에서 정상회담", "politics") is None
    assert screen("독일 ‘첫 여성 대통령’ 화두 띄운 메르켈", "politics") == "offtopic"
    assert screen("[박상영의 경제본색](24) 세 차례 미룬 가상자산 과세", "crypto") == "noise"
    assert screen("이재명 대통령님, 어떤 미래를 물려주시겠습니까?", "politics") == "noise"
    assert "nk" in watcher.salient_tokens("올해 北 소행 가상자산 해킹 피해액 90% 육박")
    assert "ustech" in watcher.salient_tokens("뉴욕증시, 하락세로 마감…나스닥 2.86%↓")
    assert "krindex" in watcher.salient_tokens("코스피, 장중 5% 출렁")


def test_paraphrase_dedup():
    tmp = tempfile.mkdtemp()
    watcher.DB_PATH = os.path.join(tmp, "watcher.db")
    watcher.CAND = os.path.join(tmp, "candidates.jsonl")
    watcher.BREAK_LOG = os.path.join(tmp, "breaking.log")
    watcher.RUN_LOG = os.path.join(tmp, "run.log")
    os.makedirs(tmp, exist_ok=True)
    con = watcher.db()
    recent = []
    stats = {"new": 0, "merged": 0, "dup": 0, "too_old": 0, "breaking": 0, "noise": 0, "offtopic": 0}
    titles = [
        "Hackers steal over $6 billion from crypto exchange Binance in staggering heist",
        "North Korean hackers blamed for $6.3B heist from crypto firm Binance",
        "Why bitcoin held up better after the record Binance hack",
        "바이낸스, 8.4조 해킹 피해 전액 보상…준비금 증명 공개",
        "삼성 사장단, 샘 올트먼 만났다…AI 미래전략 논의",
        "[속보] 삼성 사장단, 샘 올트먼 초청 AI 간담회",
        "최태원, 샘 올트먼과 오찬…SK·오픈AI 협력",
        "이 대통령, 야당 해외 순방 비판에 돼지 눈에는 돼지만 보여",
        "국힘, 순방 반박 이대통령에 비판하는 국민은 전부 돼지인가",
        "후티 반군, 아덴 대통령궁 미사일 타격",
        "폴리마켓 해킹으로 이용자 가상자산 탈취…390만 달러 피해 보상 발표",
    ]
    for i, t in enumerate(titles):
        topic = "crypto" if i < 4 or i == 10 else "ai_tech" if i < 7 else "politics"
        watcher.process(con, src(topic, "GoogleNews"), [item(t, url=f"https://news.example/{i}", publisher=f"P{i}")], recent, stats)
    con.commit()
    rows = con.execute("SELECT topic, title FROM items ORDER BY id").fetchall()
    titles_kept = [t for _topic, t in rows]
    assert stats["offtopic"] >= 1, stats
    # Hack paraphrases collapse; the compensation follow-up stays; Samsung and SK stay apart.
    hack_rows = [t for t in titles_kept if "Binance" in t or "binance" in t or "바이낸스" in t]
    assert len(hack_rows) == 2, hack_rows
    assert any("전액 보상" in t for t in titles_kept)
    assert any("폴리마켓" in t for t in titles_kept)
    assert any("삼성" in t for t in titles_kept)
    assert any("최태원" in t for t in titles_kept)
    assert sum(1 for t in titles_kept if "돼지" in t) == 1, titles_kept
    assert not any("후티" in t for t in titles_kept)
    con.close()


def test_compensation_is_not_the_hack():
    """A compensation headline seen first must not swallow the hack itself."""
    tmp = tempfile.mkdtemp()
    watcher.DB_PATH = os.path.join(tmp, "watcher.db")
    watcher.CAND = os.path.join(tmp, "c.jsonl")
    watcher.BREAK_LOG = os.path.join(tmp, "b.log")
    watcher.RUN_LOG = os.path.join(tmp, "r.log")
    con = watcher.db()
    recent, stats = [], {"new": 0, "merged": 0, "dup": 0, "too_old": 0, "breaking": 0, "noise": 0, "offtopic": 0}
    titles = [
        "바이낸스, 8.4조 해킹 피해 전액 보상…준비금 증명 공개",
        "바이낸스서 63억달러 가상자산 탈취…북한 해킹조직 개입 의혹",
        "비트코인 7만달러 붕괴…바이낸스 63억 달러 해킹 충격",
    ]
    for i, t in enumerate(titles):
        watcher.process(con, src("crypto"), [item(t, url=f"https://ex.example/c{i}", publisher=f"C{i}")], recent, stats)
    kept = [r[0] for r in con.execute("SELECT title FROM items").fetchall()]
    assert len(kept) == 2, kept
    assert any("전액 보상" in t for t in kept)
    assert any("탈취" in t or "붕괴" in t for t in kept)
    con.close()


def test_reactions_stay_separate():
    tmp = tempfile.mkdtemp()
    watcher.DB_PATH = os.path.join(tmp, "watcher.db")
    watcher.CAND = os.path.join(tmp, "c.jsonl")
    watcher.BREAK_LOG = os.path.join(tmp, "b.log")
    watcher.RUN_LOG = os.path.join(tmp, "r.log")
    con = watcher.db()
    recent, stats = [], {"new": 0, "merged": 0, "dup": 0, "too_old": 0, "breaking": 0, "noise": 0, "offtopic": 0}
    titles = [
        ("crypto", "비트코인 급락…바이낸스 63억 달러 해킹 충격"),
        ("politics", "트럼프, 바이낸스 해킹 북한 소행 여부에 모른다"),
        ("crypto", "올해 전 세계 가상자산 해킹 피해액 89%, 북한 소행"),
        ("crypto", "국내외 가상자산거래소 노린 해킹, 금전 피해로 이어졌다"),
    ]
    for i, (topic, t) in enumerate(titles):
        watcher.process(con, src(topic), [item(t, url=f"https://ex.example/r{i}", publisher=f"R{i}")], recent, stats)
    kept = [r[0] for r in con.execute("SELECT title FROM items").fetchall()]
    assert any("63억" in t for t in kept), kept
    assert any("모른다" in t for t in kept), kept
    assert any("89%" in t for t in kept), kept
    con.close()


def test_score_suppression():
    score, hits = watcher.kw_score("비트코인 급락", watcher.CFG["topics"]["crypto"]["keywords"])
    assert "비트코인" in hits and "코인" not in hits


# Day-specific names from the 2026-11-01 audit. A later story that uses one of
# these words must not be dropped because that day's label file mentioned it.
_DAY_SPECIFIC = (
    "광주", "전남", "논산", "군공항", "명태균", "윤여준", "분류체계", "6만5000",
    "O'Leary", "10월 종가", "블루스윕", "아이큐랩", "갤버즈", "이재용", "에르도안",
)


def test_opinion_noise_and_foreign_politics():
    assert watcher.CFG["cluster_window_hours"] == 24
    patterns = "\n".join(watcher.CFG["filters"]["noise_patterns"])
    for term in _DAY_SPECIFIC:
        assert term not in patterns, term
    assert "푸틴" not in watcher.CFG["filters"]["foreign_markers"]
    src_text = open(watcher.__file__, encoding="utf-8").read()
    assert "_event_anchors" not in src_text
    for term in _DAY_SPECIFIC:
        assert term not in src_text, term

    for title in (
        "[칼럼] 코스피가 흔들릴 때",
        "[사설] 반도체 수출을 두고",
        "[기고] 금리 인하를 서두를 이유",
        "[시론] 예산안의 빈칸",
        "[오피니언] 중간선거 이후",
        "[전망] 다음 주 환율",
        "[초점] 수출 통계의 착시",
        "[예능] 주말 결방 안내",
        "[이주의 은행 예금금리] 12개월 최고 연 4.10%",
        "[이주의 은행 적금금리-11월 1주] 케이뱅크",
        "주요 은행 금리표",
    ):
        assert watcher.noise_title(title), title
    assert watcher.noise_title("이번 주 금리 전망", "https://news.example.com/opinion/rates")
    assert watcher.noise_title("칼럼입니다", "https://news.example.com/column/lee")
    assert watcher.noise_title("해설", "https://news.example.com/columns/weekly")
    # A bare 전망, a section title that merely contains the word, and a quoted
    # show name are ordinary headlines.
    assert not watcher.noise_title("키옥시아 실적 전망")
    assert not watcher.noise_title("코스피 실적 전망")
    assert watcher.noise_title("[뉴욕증시-주간전망] 기술주 반등")
    assert watcher.noise_title("[증시전망] 코스피 6,000선 회복 뒤 다시 흔들")
    assert watcher.noise_title("[굿모닝 증시] 오늘 일정")
    assert watcher.noise_title("[김대호 진단] 반도체 열전")
    assert watcher.noise_title("양산시, 생성형 AI 활용 경진대회 장려상")
    assert watcher.noise_title("한국콜마, ESG 대통령표창 2관왕")
    assert watcher.noise_title("Will Solana Hit $500 by 2028?")
    assert watcher.noise_title("비트코인 1.05% 오른 7만 8,249달러")
    assert not watcher.noise_title("[오전 브리핑] 비트코인 1억425만원…한은, 가상자산 진출 현황 조사")
    assert not watcher.noise_title("Crypto: Ethereum drops 16% after record ETF outflows")
    assert not watcher.noise_title("뉴욕증시, 기술주 급락")
    assert not watcher.noise_title("‘SNL’ Weekend Update Mocks Trump’s Midterm Plea")
    for title in (
        "광주 인공지능 데이터센터 착공",
        "전남·논산 군공항 이전 논의",
        "명태균 씨 관련 수사",
        "윤여준 전 장관 정치 개혁 발언",
        "새 분류체계 발표",
        "6만5000원 돌파",
        "O'Leary says the market is open",
        "10월 종가 기준 수익률",
        "블루스윕 이후 시리즈",
        "아이큐랩 신규 서비스",
        "갤버즈 신제품 공개",
    ):
        assert not watcher.noise_title(title), title

    assert screen("푸틴 러시아 대통령, 대 유럽 전쟁 준비 발언", "politics") is None
    assert screen("에르도안 튀르키예 대통령 국빈방한…3일 李대통령과 정상회담", "politics") is None
    assert screen("李대통령, 6일 동티모르 대통령과 정상회담", "politics") is None
    assert screen("코스피 실적 전망", "markets") is None
    assert screen("[속보] 반도체 10월 수출 564억달러…2개월 연속 500억달러대", "ai_tech") is None
    assert screen("[예능] 주말 결방 안내", "politics") == "noise"


def test_session_and_print_cluster_without_merging_directions():
    """The same tape in the same direction collapses. The drop and the bounce do not."""
    tmp = tempfile.mkdtemp()
    watcher.DB_PATH = os.path.join(tmp, "watcher.db")
    watcher.CAND = os.path.join(tmp, "c.jsonl")
    watcher.BREAK_LOG = os.path.join(tmp, "b.log")
    watcher.RUN_LOG = os.path.join(tmp, "r.log")
    con = watcher.db()
    recent, stats = [], {"new": 0, "merged": 0, "dup": 0, "too_old": 0, "breaking": 0, "noise": 0, "offtopic": 0}
    titles = [
        "[속보] 코스피 1.62% 내린 5,637.77 개장",
        "코스피, 외국인·개인 매도세에 장중 1.62% 약세",
        "[속보] 코스피, 낙폭 축소해 보합권서 등락…장중 상승 전환도",
        "코스피, 장중 상승 전환해 1%대 오름세…코스닥도 반등",
        "[속보] 원·달러 환율, 9.5원 오른 1343.8원",
        "원·달러 환율 9.5원 급등, 1343.8원",
        "[속보] 李대통령 \"반도체·피지컬AI 등 3대 메가 프로젝트 21.3조 투자\"",
        "李대통령 \"AI·반도체 등 3대 메가프로젝트에 21.3조 투자\"",
        "[속보] 李대통령 \"국방예산 8.2% 증액…핵잠 확보할 것\"",
    ]
    for i, t in enumerate(titles):
        topic = "markets" if i < 6 else "politics"
        watcher.process(con, src(topic), [item(t, url=f"https://ex.example/s{i}", publisher=f"S{i}")], recent, stats)
    kept = [r[0] for r in con.execute("SELECT title FROM items ORDER BY id").fetchall()]
    assert sum("1.62%" in t or "5,637" in t for t in kept) == 1, kept
    assert sum("상승 전환" in t or "오름세" in t for t in kept) == 1, kept
    assert sum("9.5" in t or "1343.8" in t for t in kept) == 1, kept
    assert sum("21.3" in t for t in kept) == 1, kept
    assert any("8.2%" in t for t in kept), kept
    con.close()


def test_generic_similarity_keeps_distinct_events():
    """Near-copy headlines from two outlets merge. Different events do not."""
    tmp = tempfile.mkdtemp()
    watcher.DB_PATH = os.path.join(tmp, "watcher.db")
    watcher.CAND = os.path.join(tmp, "c.jsonl")
    watcher.BREAK_LOG = os.path.join(tmp, "b.log")
    watcher.RUN_LOG = os.path.join(tmp, "r.log")
    con = watcher.db()
    recent, stats = [], {"new": 0, "merged": 0, "dup": 0, "too_old": 0, "breaking": 0, "noise": 0, "offtopic": 0}
    titles = [
        ("ai_tech", "이재용, 日서 AI 반도체 승부처 점검…첨단 패키징 협력 강화"),
        ("ai_tech", "이재용, 일본 요코하마 패키징랩 첫 점검…AI 반도체 승부처 챙긴다"),
        ("politics", "에르도안 튀르키예 대통령 국빈방한…3일 李대통령과 정상회담"),
        ("politics", "李대통령, 6일 동티모르 대통령과 정상회담"),
        ("politics", "李대통령, 튀르키예·동티모르 연쇄 정상회담…원전·방산 수출 협력"),
        ("crypto", "바이낸스서 63억달러 가상자산 탈취…북한 해킹조직 개입 의혹"),
        ("crypto", "바이낸스 출금 재개…입출금 정상화"),
    ]
    for i, (topic, t) in enumerate(titles):
        watcher.process(con, src(topic), [item(t, url=f"https://ex.example/n{i}", publisher=f"N{i}")], recent, stats)
    kept = [r[0] for r in con.execute("SELECT title FROM items ORDER BY id").fetchall()]
    assert sum("이재용" in t for t in kept) == 1, kept
    assert any("튀르키예" in t or "에르도안" in t for t in kept)
    assert any("동티모르" in t for t in kept)
    assert sum(1 for t in kept if "튀르키예" in t or "에르도안" in t or "동티모르" in t) >= 2, kept
    assert any("탈취" in t or "63억" in t for t in kept)
    assert any("출금 재개" in t for t in kept), kept
    con.close()


def test_reprint_outside_24h_window():
    """A 30-hour-old annual-stat row is outside the window, and the reprint is not the hack."""
    tmp = tempfile.mkdtemp()
    watcher.DB_PATH = os.path.join(tmp, "watcher.db")
    watcher.CAND = os.path.join(tmp, "c.jsonl")
    watcher.BREAK_LOG = os.path.join(tmp, "b.log")
    watcher.RUN_LOG = os.path.join(tmp, "r.log")
    con = watcher.db()
    recent, stats = [], {"new": 0, "merged": 0, "dup": 0, "too_old": 0, "breaking": 0, "noise": 0, "offtopic": 0}
    first = [
        "바이낸스서 63억달러 가상자산 탈취…북한 해킹조직 개입 의혹",
        "올해 전 세계 가상자산 해킹 피해액 89%, 북한 소행",
    ]
    for i, t in enumerate(first):
        watcher.process(con, src("crypto"), [item(t, url=f"https://ex.example/a{i}", publisher=f"A{i}", hours_ago=2)], recent, stats)
    # Age only yesterday's annual-stat row. The hack stays inside the window,
    # so a merge into the hack would still be visible.
    recent[1] = (*recent[1][:3], recent[1][3] - timedelta(hours=30), *recent[1][4:])
    later = item("\"올해 가상자산 해킹 피해 89%, 북한 소행 추정\" - 머니투데이",
                 url="https://ex.example/a2", publisher="머니투데이", hours_ago=1)
    watcher.process(con, src("crypto"), [later], recent, stats)
    kept = [r[0] for r in con.execute("SELECT title FROM items").fetchall()]
    assert len(kept) == 3, kept
    assert any("63억" in t or "탈취" in t for t in kept)
    assert sum("89%" in t for t in kept) == 2, kept
    con.close()


def test_labeled_day_holdout():
    """Afternoon of 2026-11-01 is a holdout: do not lose a large share of true stories.

    The exact false-positive rate is not pinned. Thresholds were chosen on the
    morning slice, and this check only guards the safety property.
    """
    import json
    fixture = os.path.join(os.path.dirname(__file__), "tests", "fixtures", "news-fp-2026-11-01.json")
    data = json.load(open(fixture, encoding="utf-8"))
    rows = sorted(data["items"], key=lambda r: (r["first_seen"], r["id"]))
    cut = datetime.fromisoformat("2026-11-01T06:00:00+00:00")
    saved = (watcher.now, watcher.DB_PATH, watcher.CAND, watcher.BREAK_LOG, watcher.RUN_LOG)
    clock = {"t": None}
    tmp = tempfile.mkdtemp()
    try:
        watcher.now = lambda: clock["t"]
        watcher.DB_PATH = os.path.join(tmp, "watcher.db")
        watcher.CAND = os.path.join(tmp, "c.jsonl")
        watcher.BREAK_LOG = os.path.join(tmp, "b.log")
        watcher.RUN_LOG = os.path.join(tmp, "r.log")
        con = watcher.db()
        recent = []
        weights = {s["name"]: s["weight"] for s in watcher.CFG["sources"]}
        clusters = {}
        for j in rows:
            clock["t"] = datetime.fromisoformat(j["first_seen"])
            src_row = {"name": j["source"], "topic": j["topic"], "type": "rss",
                       "weight": weights.get(j["source"], 1.0)}
            it = dict(title=j["title"], url=j["url"],
                      published=datetime.fromisoformat(j["published"]),
                      publisher=j["publisher"] or j["source"], engagement=None)
            st = {"new": 0, "merged": 0, "dup": 0, "too_old": 0, "breaking": 0, "noise": 0, "offtopic": 0}
            watcher.process(con, src_row, [it], recent, st)
            un = it["url"] if str(it["url"]).startswith("signal://") else watcher.norm_url(it["url"])
            hit = con.execute("SELECT item_id FROM aliases WHERE url_norm=?", (un,)).fetchone()
            if hit:
                clusters.setdefault(hit[0], []).append(j)
        afternoon_tp = [j for j in rows if j["verdict"] == "정탐" and datetime.fromisoformat(j["first_seen"]) >= cut]
        assert len(afternoon_tp) == 60
        id_to_cluster = {}
        for iid, members in clusters.items():
            for m in members:
                id_to_cluster[m["id"]] = iid
        lost = 0
        for j in afternoon_tp:
            iid = id_to_cluster.get(j["id"])
            if iid is None:
                lost += 1
                continue
            tps = [m for m in clusters[iid] if m["verdict"] == "정탐"]
            if len(tps) != 1:
                lost += 1
        assert lost / len(afternoon_tp) <= 0.25, lost
        def true_hits(words):
            return {iid for iid, members in clusters.items()
                    if any(m["verdict"] == "정탐" and any(w in m["title"] for w in words) for m in members)}
        turkey = true_hits(("에르도안", "튀르키예"))
        timor = true_hits(("동티모르",))
        assert turkey and timor and not (turkey & timor)
        con.close()
    finally:
        watcher.now, watcher.DB_PATH, watcher.CAND, watcher.BREAK_LOG, watcher.RUN_LOG = saved


def test_tapes_cluster_and_reactions_stay_under_scout():
    """One tape per crash, vote, and appointment. Reactions stay under the scout line."""
    tmp = tempfile.mkdtemp()
    watcher.DB_PATH = os.path.join(tmp, "watcher.db")
    watcher.CAND = os.path.join(tmp, "c.jsonl")
    watcher.BREAK_LOG = os.path.join(tmp, "b.log")
    watcher.RUN_LOG = os.path.join(tmp, "r.log")
    con = watcher.db()
    recent, stats = [], {"new": 0, "merged": 0, "dup": 0, "too_old": 0, "breaking": 0, "noise": 0, "offtopic": 0}
    titles = [
        ("markets", "[속보] 증시 급락에 코스피 서킷브레이커 발동…5천피 밑으로"),
        ("markets", "[속보] 코스피 '서킷브레이커' 발동...5000피 붕괴"),
        ("ai_tech", "[속보] 반도체주 추락…삼성전자 9%·SK하이닉스 11%대 급락"),
        ("markets", "[속보] 정부, 증시 급락에 긴급 대응‥신용융자 담보비율 의무 3개월 면제"),
        ("markets", "[속보] F4 \"증시 변동성 확대하면 시장안정 추가 조치\""),
        ("politics", "[속보] 조작기소 특검법안 여당 주도 국회 통과"),
        ("politics", "[속보] 조작기소 특검법 여권 주도로 국회 본회의 통과"),
        ("politics", "국힘 \"조작특검으로 대통령 셀프면죄\"…특검법 반대 여론전"),
        ("politics", "[속보] 이재명 대통령, 법무부 장관 후보자에 이용철 지명"),
        ("politics", "[속보] 이 대통령, 법무장관 이용철…국세청장 안덕수 지명"),
        ("politics", "[속보] 이 대통령, KBS 사장 후보자 인사청문요청안 재가"),
        ("crypto", "비트코인, 7만6000달러 하회…기술주 급락에 약세"),
        ("crypto", "Bitcoin Slumps Below US$76,000 as the selloff deepens"),
        ("crypto", "비트코인 ETF 4억3020만달러 순유출"),
        ("crypto", "가상자산 거래소 고팍스, 해킹 위협에 입출금 매매 중단"),
        ("crypto", "고팍스, 해킹 예고 협박에 가상자산 거래 전면 중단"),
        ("ai_tech", "오픈AI, 해킹 예방하는 보안 모델 GPT-6 사이버 공개"),
        ("ai_tech", "오픈AI, 해킹 취약점을 찾는 GPT-6 Cyber 공개"),
    ]
    for i, (topic, t) in enumerate(titles):
        watcher.process(con, src(topic), [item(t, url=f"https://ex.example/tape{i}", publisher=f"S{i}")], recent, stats)
    kept = [r[0] for r in con.execute("SELECT title FROM items ORDER BY id").fetchall()]
    assert sum("서킷브레이커" in t or "반도체주" in t for t in kept) == 1, kept
    assert any("담보비율" in t for t in kept), kept
    assert any("시장안정" in t for t in kept), kept
    assert sum("특검" in t and "통과" in t for t in kept) == 1, kept
    assert any("여론전" in t for t in kept), kept
    assert sum("지명" in t for t in kept) == 1, kept
    assert any("청문" in t for t in kept), kept
    assert sum("76000" in t.replace(",", "") or "7만6000" in t or "76,000" in t for t in kept) == 1, kept
    assert any("순유출" in t for t in kept), kept
    assert sum("고팍스" in t for t in kept) == 1, kept
    assert sum("GPT-6" in t for t in kept) == 1, kept

    def scout(title, topic, cluster_n=1):
        _topic, kw = watcher.classify(title, topic)
        row = {"title": title, "source": "T", "topic": topic, "published": watcher.now().isoformat()}
        return watcher.score_item(row, 2.0, kw, None, cluster_n)[0]

    # Two outlets of the same crash clear the breaking line. A reaction does not, however many outlets repeat it.
    assert scout("[속보] 코스피 서킷브레이커 발동…5천피 붕괴", "markets", 2) >= 9
    assert scout("[속보] 정부, 증시 급락에 신용융자 담보비율 의무 3개월 면제", "markets", 2) >= 9
    assert scout("[속보] 한동훈, 코스피 5000선 붕괴에 대통령이 직접 답하라", "politics", 6) <= 7.4
    assert watcher.stress_tag("[속보]민주 압승·유가 급락에 다우 3% 급등…반도체는 약세") is None
    assert watcher.stress_tag("[속보] 코스피 서킷브레이커 발동") == "kr-selloff"
    assert scout("증시급락 전문가 긴급진단", "markets", 4) <= 7.4
    assert scout("DoorDash to Acquire Instacart | Tech Today", "ai_tech", 3) <= 7.4
    assert scout("국민은행 아동수당통장 출시…최고 연 3.1% 금리", "markets", 3) <= 7.4
    tape = con.execute("SELECT score FROM items WHERE title LIKE '%서킷브레이커%'").fetchone()
    assert tape[0] >= 8, tape
    con.close()


def test_nov2_holdout_keeps_true_stories():
    """Afternoon of 2026-11-02 is held out. True stories in that slice stay stored."""
    import json
    fixture = os.path.join(os.path.dirname(__file__), "tests", "fixtures", "news-fp-2026-11-02.json")
    rows = sorted(json.load(open(fixture, encoding="utf-8"))["items"], key=lambda r: (r["first_seen"], r["id"]))
    cut = datetime.fromisoformat("2026-11-02T06:00:00+00:00")
    saved = (watcher.now, watcher.DB_PATH, watcher.CAND, watcher.BREAK_LOG, watcher.RUN_LOG)
    clock = {"t": None}
    tmp = tempfile.mkdtemp()
    try:
        watcher.now = lambda: clock["t"]
        watcher.DB_PATH = os.path.join(tmp, "watcher.db")
        watcher.CAND = os.path.join(tmp, "c.jsonl")
        watcher.BREAK_LOG = os.path.join(tmp, "b.log")
        watcher.RUN_LOG = os.path.join(tmp, "r.log")
        con = watcher.db()
        recent = []
        weights = {s["name"]: s["weight"] for s in watcher.CFG["sources"]}
        clusters = {}
        for j in rows:
            clock["t"] = datetime.fromisoformat(j["first_seen"])
            src_row = {"name": j["source"], "topic": j["topic"], "type": "rss",
                       "weight": weights.get(j["source"], 1.0)}
            it = dict(title=j["title"], url=j["url"],
                      published=datetime.fromisoformat(j["published"]),
                      publisher=j["publisher"] or j["source"], engagement=None)
            st = {"new": 0, "merged": 0, "dup": 0, "too_old": 0, "breaking": 0, "noise": 0, "offtopic": 0}
            watcher.process(con, src_row, [it], recent, st)
            un = it["url"] if str(it["url"]).startswith("signal://") else watcher.norm_url(it["url"])
            hit = con.execute("SELECT item_id FROM aliases WHERE url_norm=?", (un,)).fetchone()
            if hit:
                clusters.setdefault(hit[0], []).append(j)
        afternoon_tp = [j for j in rows if j["verdict"] == "정탐" and datetime.fromisoformat(j["first_seen"]) >= cut]
        assert len(afternoon_tp) == 32
        id_to = {}
        for iid, members in clusters.items():
            for m in members:
                id_to[m["id"]] = iid
        lost = 0
        for j in afternoon_tp:
            iid = id_to.get(j["id"])
            if iid is None:
                lost += 1
                continue
            tps = [m for m in clusters[iid] if m["verdict"] == "정탐"]
            if len(tps) != 1:
                lost += 1
        assert lost == 0, lost
        con.close()
    finally:
        watcher.now, watcher.DB_PATH, watcher.CAND, watcher.BREAK_LOG, watcher.RUN_LOG = saved


if __name__ == "__main__":
    test_hangul_false_friends()
    test_relevance_gates()
    test_paraphrase_dedup()
    test_compensation_is_not_the_hack()
    test_reactions_stay_separate()
    test_score_suppression()
    test_opinion_noise_and_foreign_politics()
    test_session_and_print_cluster_without_merging_directions()
    test_generic_similarity_keeps_distinct_events()
    test_reprint_outside_24h_window()
    test_labeled_day_holdout()
    test_nov2_holdout_keeps_true_stories()
    test_tapes_cluster_and_reactions_stay_under_scout()
    print("ok")
