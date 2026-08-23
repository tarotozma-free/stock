# 미국주식 관심종목 일일 리포트

미국장(월~금) 마감 다음날 아침(한국시간 화~토 07:00)에 관심종목·보유종목 시세를
자동으로 갱신해 GitHub Pages 페이지에 쌓아두는 시스템입니다. 이메일 발송은 선택
사항이고(기본은 꺼져 있음), 그냥 매일 `report.html`에 접속해서 보는 방식이 기본
사용법입니다. 과거 날짜는 히스토리에서 언제든 다시 볼 수 있습니다.

## 구성

- `supabase/schema.sql` — DB 스키마 (watchlist / holdings / daily_reports / report_items / send_log)
- `scripts/generate_report.py` — 시세·기술적 지표 수집 → 매수/매도 근접도 계산 → Supabase 저장 (이메일은 선택)
- `.github/workflows/daily-report.yml` — 화~토 07:00 KST 자동 실행 (GitHub Actions)
- `docs/` — GitHub Pages로 서빙되는 리포트 뷰어(`report.html`, `history.html`)와
  관심종목/보유종목 관리 앱(`manage.html`, 이메일+비밀번호 로그인 필요)

## 매수 근접도 점수 (핵심 로직)

`watchlist`에는 티커와 자유 메모(`thesis`)만 있고, 적정 PER·매수 목표가처럼 판단이 필요한
값은 사람이 손으로 넣지 않습니다. 대신 매일 아래 네 가지를 조합한 **0~100점 "매수 근접도"**를
모든 종목에 동일한 계산식으로 자동 산출합니다 (`scripts/generate_report.py`의 `compute_buy_score`):

- **지지선 근접도(40점)** — 직전 스윙저점·120일선·200일선·52주 저가 중 현재가 바로 아래
  가장 가까운 지지선에 얼마나 근접했는지
- **눌림목 깊이(25점)** — 직전 고점 대비 얼마나 빠졌는지 (많이 빠질수록 높은 점수)
- **PEG 밸류에이션(20점)** — PER÷이익성장률이 낮을수록(성장 대비 저렴할수록) 높은 점수.
  "적정 PER"을 사람이 정하지 않아도 되는 객관적 지표
- **추세 구조(15점)** — 정배열/골든크로스면 가산, 역배열/데드크로스면 감산

65점 이상 "매수 근접(강한 신호)", 40~64점 "관망(접근 중)", 그 미만 "아직 매수권 아님"으로 분류합니다.
점수 구성(`buy_score_detail`)이 그대로 저장돼 왜 그 점수가 나왔는지 항상 확인할 수 있습니다.

**매도 근접도**도 완전히 대칭되는 계산(`compute_sell_score`)으로 함께 산출됩니다 — 저항선 근접도,
급등 과열도(직전 저점 대비 얼마나 올랐는지), PEG(비쌀수록 높은 점수), 역배열/데드크로스 가산.

**알려진 한계**: PEG는 최근 실적이 부진하거나 적자인 종목(예: 턴어라운드 성장주)에서 왜곡될 수 있습니다
— 트레일링 이익 기준이라 "지금은 안 좋아도 미래 EPS가 급성장할 것"이라는 전망을 반영하지 못합니다.

그 외 함께 담기는 항목:
- 종가 / 전일대비 등락률 / 52주 고가·저가 / PER(TTM) (Finnhub)
- 20/60/120/200일 이동평균, 정배열/역배열/혼조 판정, 골든/데드크로스
- 직전 스윙 고점/저점 (좌우 5거래일 프랙탈 기준)
- 애널리스트 매수/보유/매도 컨센서스 (목표주가는 Finnhub 무료 플랜에서 막혀 있어 비어 있음)
- **이벤트 뉴스**: 등락률이 ±5%(코드 상단 `EVENT_MOVE_THRESHOLD_PCT`) 이상인 종목만
  최근 뉴스 헤드라인 최대 3건을 자동 첨부 (Finnhub company-news)

가격 시세·PER·PEG·애널리스트·뉴스는 Finnhub, 1년치 일봉(이동평균·크로스·스윙 계산용)은
Yahoo Finance 차트 API(무료, 키 불필요)에서 가져옵니다.

## 오늘의 추천 (나스닥100 스캔)

관심종목/보유종목에만 국한되지 않고, 매일 나스닥 공식 API에서 실시간으로 나스닥100 구성종목
전체(약 102개)를 가져와 이미 추적 중인 종목을 제외한 뒤 **완전히 동일한 `compute_buy_score`**로
스캔해서 매수 근접도 상위 5개를 뽑습니다 (`scripts/generate_report.py`의 `scan_top_picks`).
별도의 추천 로직이나 종목 선정 기준은 없고, 스캔 대상 풀만 넓어진 것뿐입니다.

Finnhub 무료 플랜의 분당 60콜 제한 때문에 호출 사이 최소 간격을 두고 있어(`FINNHUB_CALL_DELAY_SEC`),
이 스캔만으로 실행 시간이 4~5분 정도 늘어납니다. 나스닥100 목록 조회가 실패하면 코드에 내장된
스냅샷(마지막으로 확인된 시점 기준)으로 자동 대체됩니다.

## 설정 순서

### 1. Supabase 프로젝트

1. https://supabase.com 에서 새 프로젝트 생성
2. SQL Editor에서 `supabase/schema.sql` 전체 실행
3. Project Settings > API 에서 다음 두 값을 확인
   - `Project URL` → `SUPABASE_URL`
   - `anon public` key → `docs/config.js`에 넣을 값
   - `service_role` key → `SUPABASE_SERVICE_KEY` (절대 공개 저장소나 docs/에 넣지 말 것)
4. `watchlist` 테이블에서 관심종목 티커를 원하는 대로 추가/삭제 (예시로 CAT/NVDA/PLTR이 들어있음).
   `thesis`는 자유 메모 칸이고 계산에는 쓰이지 않습니다 — `manage.html`에서 편하게 편집 가능
5. 실제 보유 중인 종목은 `holdings` 테이블에 티커/수량/평단가를 기록 — **관심종목에 없어도 자동으로
   리포트에 포함되고 평단가 대비 수익률(`pnl_pct`/`pnl_abs`)까지 계산됩니다**

### 1-1. 관리 앱(manage.html) 접근 잠그기 — 중요

`manage.html`은 이메일+비밀번호 로그인 후 watchlist/holdings에 직접 쓰기(추가/수정/삭제)를
할 수 있는 페이지라, 본인 외에는 로그인 자체가 안 되게 막아야 합니다. 로그인은 한 번만 하면
그 브라우저엔 세션이 계속 유지되므로(로그아웃 전까지) 매번 이메일을 확인할 필요는 없습니다.

1. Supabase Dashboard > Authentication > Sign In / Providers (또는 Settings) 에서
   **"Allow new user signups"(신규 가입 허용)를 끕니다.** 이렇게 해두면 본인이 만든 계정
   외에는 로그인 자체가 불가능합니다.
2. Authentication > Users 에서 **본인 이메일 계정을 미리 하나 추가**합니다
   (Add user > 이메일 입력 + 기억할 수 있는 비밀번호 설정 + "Auto Confirm User" 체크).
   이 비밀번호로 `manage.html`에서 바로 로그인합니다.
3. `manage.html`에서 2번에 등록한 이메일/비밀번호로 로그인하면 되고, 그 외 계정은 로그인 자체가
   실패합니다.

### 2. Finnhub API 키

https://finnhub.io 무료 가입 후 API 키 발급 → `FINNHUB_API_KEY`

### 3. Gmail 발신 계정 (선택 — 이메일 알림을 원할 때만)

기본값은 이메일 발송 없이 DB 저장만 합니다. 그냥 매일 웹페이지로 확인하는 방식이면 이 단계는
건너뛰어도 됩니다. 이메일도 받고 싶으면:

1. 발신용 Gmail 계정에서 2단계 인증 활성화
2. https://myaccount.google.com/apppasswords 에서 앱 비밀번호 생성 → `GMAIL_APP_PASSWORD`
3. `GMAIL_USER`는 이 Gmail 주소, `RECIPIENT_EMAIL`은 리포트 받을 주소(같아도 무방)
4. 아래 5단계에서 이 3개 Secrets를 추가로 등록하면 자동으로 이메일 발송이 켜집니다

### 4. GitHub 저장소 & Pages

1. 이 폴더를 새 GitHub 저장소로 push
2. 저장소 Settings > Pages 에서 소스를 `main` 브랜치 `/docs` 폴더로 설정
3. 공개된 Pages 주소를 확인해 `REPORT_BASE_URL`에 `.../docs` 형태로 설정
   (예: `https://username.github.io/repo-name/docs`)
4. `docs/config.js`의 `url`, `anonKey`를 실제 Supabase 값으로 수정 후 커밋

### 5. GitHub Actions Secrets 등록

저장소 Settings > Secrets and variables > Actions 에서 등록 (필수 4개):

- `SUPABASE_URL`
- `SUPABASE_SERVICE_KEY`
- `FINNHUB_API_KEY`
- `REPORT_BASE_URL`

이메일도 받고 싶으면 추가로 (선택):

- `GMAIL_USER`, `GMAIL_APP_PASSWORD`, `RECIPIENT_EMAIL`

### 6. 동작 확인

- Actions 탭에서 `Daily Stock Report` 워크플로우를 `Run workflow`로 수동 실행해 테스트
- `report.html`에 그날 데이터가 갱신됐는지 확인 (이메일 Secrets를 안 넣었다면 이메일은 안 옴 — 정상)
- 정상 확인되면 이후엔 매주 화~토 07:00 KST에 자동 실행됨

## 로컬 테스트

```bash
pip install requests
cp .env.example .env  # 값 채우기
set -a; source .env; set +a  # 또는 각 변수를 직접 export
python scripts/generate_report.py
```

## 스케줄 로직

미국 정규장은 월~금 09:30~16:00 ET에 열립니다. 마감(16:00 ET)은 한국시간으로
새벽 5~6시(서머타임 여부에 따라 다름)이므로, 같은 날 저녁 UTC 22:00(=한국시간
다음날 07:00)에 실행하면 전날 미국 마감 데이터를 안전하게 가져올 수 있습니다.
이 시차 때문에 발송일은 한국 기준 화~토가 됩니다.

## 향후 확장 아이디어

- 미국 증시 휴장일 캘린더 체크 후 휴장일엔 실행 스킵
- 실패 시 재시도 로직 강화
- 뉴스 필터링 정교화, 거래량 등 리포트 항목 확대
