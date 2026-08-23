# 미국주식 관심종목 일일 리포트

미국장(월~금) 마감 다음날 아침(한국시간 화~토 07:00)에 관심종목 시세를 정리해
이메일로 리포트 링크를 보내주는 자동화 시스템입니다. 리포트는 Supabase에 날짜별로
쌓이고, GitHub Pages 페이지에서 링크로 바로 열람하거나 과거 날짜를 히스토리에서
찾아볼 수 있습니다.

## 구성

- `supabase/schema.sql` — DB 스키마 (watchlist / holdings / daily_reports / report_items / send_log)
- `scripts/generate_report.py` — 시세·기술적 지표 수집 → Supabase 저장 → 이메일 발송
- `.github/workflows/daily-report.yml` — 화~토 07:00 KST 자동 실행 (GitHub Actions)
- `docs/` — GitHub Pages로 서빙되는 리포트 뷰어(`report.html`, `history.html`)와
  관심종목/보유종목 관리 앱(`manage.html`, 매직링크 로그인 필요)

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
5. 실제 보유 중인 종목은 `holdings` 테이블에 티커/수량/평단가를 기록 (선택 사항, 아직 리포트에는
   반영되지 않고 테이블만 준비된 상태 — 다음 단계에서 연동 예정)

### 1-1. 관리 앱(manage.html) 접근 잠그기 — 중요

`manage.html`은 매직링크 로그인 후 watchlist/holdings에 직접 쓰기(추가/수정/삭제)를
할 수 있는 페이지라, 본인 외에는 로그인 자체가 안 되게 막아야 합니다.

1. Supabase Dashboard > Authentication > Sign In / Providers (또는 Settings) 에서
   **"Allow new user signups"(신규 가입 허용)를 끕니다.** 이렇게 해두면 신규 이메일로
   `signInWithOtp`를 호출해도 계정이 자동 생성되지 않습니다.
2. Authentication > Users 에서 **본인 이메일 계정을 미리 하나 추가**합니다
   (Add user > 본인 이메일 입력, 비밀번호는 아무거나/랜덤이어도 무방 — 매직링크로만 로그인할 것이므로).
3. Authentication > URL Configuration 에서 **Redirect URLs**에 `manage.html`의 실제 주소
   (예: `https://username.github.io/repo-name/docs/manage.html`)를 추가합니다.
   (안 해두면 매직링크 클릭 후 로그인이 안 붙습니다.)
4. 이후 `manage.html`에서 본인 이메일로 로그인 링크를 요청하면 정상 로그인되고,
   그 외 이메일은 계정이 없어 로그인 링크 요청이 실패합니다.

### 2. Finnhub API 키

https://finnhub.io 무료 가입 후 API 키 발급 → `FINNHUB_API_KEY`

### 3. Gmail 발신 계정

1. 발신용 Gmail 계정에서 2단계 인증 활성화
2. https://myaccount.google.com/apppasswords 에서 앱 비밀번호 생성 → `GMAIL_APP_PASSWORD`
3. `GMAIL_USER`는 이 Gmail 주소, `RECIPIENT_EMAIL`은 리포트 받을 주소(같아도 무방)

### 4. GitHub 저장소 & Pages

1. 이 폴더를 새 GitHub 저장소로 push
2. 저장소 Settings > Pages 에서 소스를 `main` 브랜치 `/docs` 폴더로 설정
3. 공개된 Pages 주소를 확인해 `REPORT_BASE_URL`에 `.../docs` 형태로 설정
   (예: `https://username.github.io/repo-name/docs`)
4. `docs/config.js`의 `url`, `anonKey`를 실제 Supabase 값으로 수정 후 커밋

### 5. GitHub Actions Secrets 등록

저장소 Settings > Secrets and variables > Actions 에서 아래 값을 모두 등록:

- `SUPABASE_URL`
- `SUPABASE_SERVICE_KEY`
- `FINNHUB_API_KEY`
- `GMAIL_USER`
- `GMAIL_APP_PASSWORD`
- `RECIPIENT_EMAIL`
- `REPORT_BASE_URL`

### 6. 동작 확인

- Actions 탭에서 `Daily Stock Report` 워크플로우를 `Run workflow`로 수동 실행해 테스트
- 이메일이 오는지, 링크의 리포트 페이지가 뜨는지 확인
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

- 미국 증시 휴장일 캘린더 체크 후 휴장일엔 발송 스킵
- `holdings`(보유종목) 평단가 대비 현재가 수익률을 리포트/앱에 반영
- 뉴스 헤드라인, 거래량 등 리포트 항목 확대
- 실패 시 재시도 로직 강화
