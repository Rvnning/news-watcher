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


if __name__ == "__main__":
    test_hangul_false_friends()
    test_relevance_gates()
    test_paraphrase_dedup()
    test_compensation_is_not_the_hack()
    test_reactions_stay_separate()
    test_score_suppression()
    print("ok")
