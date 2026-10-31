# news-watcher

토큰·LLM 없이 동작하는 뉴스 감시기입니다. 표준 라이브러리만 쓰는 Python으로 약 31개 뉴스 소스(RSS, Hacker News, Reddit, Binance 공지 등)를 폴링해서 규칙 기반으로 점수를 매기고, 중복을 묶은 뒤 '속보'급 기사를 기록합니다.

## 동작 방식
- **GitHub Actions**(`.github/workflows/watch.yml`)가 15분마다(cron `7,22,37,52 * * * *`, UTC) 실행됩니다. 공개 저장소라서 비용은 $0입니다.
- 실행 순서: `state.py load`로 `state/state.jsonl`에서 SQLite DB를 복원합니다 → `watcher.py`로 한 주기를 실행합니다 → `state.py dump`로 정리한 뒤 다시 저장합니다 → 바뀐 내용이 있을 때만 `github-actions[bot]` 이름으로 커밋하고 푸시합니다.
- 저장소 크기를 작게 유지하려고, 기사 상태는 최근 **3일**, `state/breaking.log`는 최근 **7일**만 남깁니다. `candidates.jsonl`와 실행 로그는 커밋하지 않습니다(로그는 각 Actions 실행 로그에서 볼 수 있습니다).
- 실패한 소스는 기존과 같이 백오프됩니다(연속 실패 시 몇 주기 건너뜀).
- `fire_webhook()`은 `WEBHOOK_URL` 환경변수가 없으면 아무것도 하지 않습니다. 현재 시크릿을 설정하지 않았으므로 **외부로 메시지를 보내지 않습니다.**

## 결과 보기
- 속보 기록: 저장소의 `state/breaking.log`(한 줄에 JSON 하나)를 보면 됩니다.
- 요약 리포트(로컬):
  ```bash
  git pull
  python3 state.py load          # state/state.jsonl -> data/watcher.db
  python3 report.py --hours 24   # --topic crypto, --queue breaking 등
  ```
- 실행별 소스 성공/실패: Actions 탭에서 각 실행의 "Run watcher" 단계 로그를 보면 됩니다.

## 일시정지 / 재개
- Actions 탭 → `news-watch` → `...` → **Disable workflow**를 누르면 멈추고, **Enable workflow**를 누르면 다시 돌아갑니다.
- 또는 `gh workflow disable news-watch` / `gh workflow enable news-watch` 명령을 씁니다.
- 수동 실행: `gh workflow run news-watch`

## 주의: 60일 비활성 규칙
GitHub는 공개 저장소에서 **60일 동안 저장소 활동이 없으면** 예약(schedule) 워크플로를 자동으로 비활성화합니다. 이 워크플로는 상태가 바뀔 때마다 커밋하기 때문에 보통은 계속 활성 상태로 유지됩니다. 그래도 멈췄다면 Actions 탭에서 다시 Enable 하면 됩니다.
