"""
관심종목 일일 리포트 생성 스크립트.

동작:
1. Supabase에서 활성 관심종목(watchlist) + 보유종목(holdings) 조회
2. Finnhub/Yahoo Finance에서 시세·기술적 지표·PEG·뉴스 조회 후 매수/매도 근접도 계산
3. Supabase에 해당 미국 거래일(daily_reports/report_items)로 저장 — report.html이 이 데이터를 그대로 보여줌
4. (선택) Gmail 계정이 설정돼 있으면 이메일도 발송. 안 넣으면 이메일 없이 DB 저장만 하고 끝남

필요한 환경변수:
  SUPABASE_URL, SUPABASE_SERVICE_KEY  - Supabase 프로젝트 설정 > API
  FINNHUB_API_KEY                     - finnhub.io 무료 API 키
  REPORT_BASE_URL                     - GitHub Pages 주소 (예: https://<user>.github.io/<repo>/docs)

선택 환경변수 (이메일 발송을 원할 때만):
  GMAIL_USER, GMAIL_APP_PASSWORD      - Gmail 발신 계정 + 앱 비밀번호(2단계 인증 필요)
  RECIPIENT_EMAIL                     - 리포트 받을 이메일 (기본값: GMAIL_USER)
"""

import os
import re
import smtplib
import sys
import time
from datetime import datetime, timedelta
from email.mime.text import MIMEText
from zoneinfo import ZoneInfo

import requests

SUPABASE_URL = os.environ["SUPABASE_URL"].rstrip("/")
SUPABASE_SERVICE_KEY = os.environ["SUPABASE_SERVICE_KEY"]
FINNHUB_API_KEY = os.environ["FINNHUB_API_KEY"]
REPORT_BASE_URL = os.environ["REPORT_BASE_URL"].rstrip("/")

GMAIL_USER = os.environ.get("GMAIL_USER")
GMAIL_APP_PASSWORD = os.environ.get("GMAIL_APP_PASSWORD")
RECIPIENT_EMAIL = os.environ.get("RECIPIENT_EMAIL", GMAIL_USER)
EMAIL_ENABLED = bool(GMAIL_USER and GMAIL_APP_PASSWORD)

EVENT_MOVE_THRESHOLD_PCT = 5.0  # 등락률이 이 값 이상이면 "이벤트 있었던 종목"으로 보고 뉴스 첨부
TOP_PICKS_COUNT = 5             # "오늘의 추천" 탭에 보여줄 개수
FINNHUB_CALL_DELAY_SEC = 1.05   # 무료 티어 분당 60콜 제한을 넘지 않도록 호출 사이 최소 대기

SB_HEADERS = {
    "apikey": SUPABASE_SERVICE_KEY,
    "Authorization": f"Bearer {SUPABASE_SERVICE_KEY}",
    "Content-Type": "application/json",
}


def sb_get(path, params=None):
    r = requests.get(f"{SUPABASE_URL}/rest/v1/{path}", headers=SB_HEADERS, params=params, timeout=30)
    r.raise_for_status()
    return r.json()


def sb_post(path, body, prefer="return=representation"):
    headers = {**SB_HEADERS, "Prefer": prefer}
    r = requests.post(f"{SUPABASE_URL}/rest/v1/{path}", headers=headers, json=body, timeout=30)
    r.raise_for_status()
    return r.json() if r.text else None


def get_watchlist():
    return sb_get(
        "watchlist",
        params={"active": "eq.true", "select": "ticker,display_name,thesis"},
    )


def get_holdings():
    return sb_get("holdings", params={"select": "ticker,quantity,avg_buy_price"})


def merge_watchlist_and_holdings(watchlist, holdings):
    """보유종목은 관심종목에 없어도 리포트에 자동 포함 — 같은 계산 파이프라인을 그대로 탄다."""
    by_ticker = {row["ticker"]: dict(row, quantity=None, avg_buy_price=None) for row in watchlist}
    for h in holdings:
        ticker = h["ticker"]
        if ticker in by_ticker:
            by_ticker[ticker]["quantity"] = h.get("quantity")
            by_ticker[ticker]["avg_buy_price"] = h.get("avg_buy_price")
        else:
            by_ticker[ticker] = {
                "ticker": ticker,
                "display_name": None,
                "thesis": None,
                "quantity": h.get("quantity"),
                "avg_buy_price": h.get("avg_buy_price"),
            }
    return list(by_ticker.values())


# 2026-08-20 기준 스냅샷 — 나스닥 API 실시간 조회가 실패할 때만 쓰는 비상용 대체 리스트.
_NASDAQ100_FALLBACK = [
    "AAPL", "AMAT", "AMGN", "CMCSA", "INTC", "KLAC", "PCAR", "CTAS", "PAYX", "LRCX",
    "ADSK", "ROST", "MNST", "MSFT", "ADBE", "FAST", "CSCO", "REGN", "IDXX", "VRTX",
    "ODFL", "QCOM", "GILD", "SNPS", "SBUX", "INTU", "MCHP", "ORLY", "COST", "CPRT",
    "ASML", "TTWO", "AMZN", "MSTR", "NVDA", "BKNG", "ISRG", "MRVL", "ADI", "AEP",
    "AMD", "ADP", "CDNS", "CSX", "HON", "MAR", "MU", "XEL", "EXC", "PEP",
    "ROP", "TER", "TXN", "WDC", "WMT", "AXON", "MDLZ", "NFLX", "STX", "ALNY",
    "GOOGL", "MPWR", "DXCM", "TMUS", "MELI", "KDP", "NBIS", "AVGO", "FTNT", "TSLA",
    "NXPI", "FANG", "META", "PANW", "WDAY", "GOOG", "PYPL", "SHOP", "KHC", "LITE",
    "CCEP", "BKR", "PDD", "CRWD", "DDOG", "RKLB", "PLTR", "ABNB", "DASH", "APP",
    "CEG", "WBD", "GEHC", "LIN", "ARM", "TRI", "FER", "ALAB", "SNDK", "CRWV",
    "SPCX", "HONA",
]


def get_nasdaq100_tickers():
    """나스닥 공식 API에서 실시간으로 현재 나스닥100 구성종목을 가져온다 (키 불필요).
    실패하면 마지막으로 확인된 스냅샷으로 대체한다 (지수 리밸런싱 반영이 늦을 수 있음)."""
    try:
        r = requests.get(
            "https://api.nasdaq.com/api/quote/list-type/nasdaq100",
            headers={"User-Agent": "Mozilla/5.0"},
            timeout=15,
        )
        r.raise_for_status()
        rows = r.json()["data"]["data"]["rows"]
        tickers = [row["symbol"] for row in rows if row.get("symbol")]
        return tickers or _NASDAQ100_FALLBACK
    except Exception:
        return _NASDAQ100_FALLBACK


def scan_top_picks(exclude_tickers, report_date, limit=TOP_PICKS_COUNT):
    """관심종목·보유종목이 아닌 나스닥100 종목 중 매수 근접도가 가장 높은 상위 N개를 찾는다.
    watchlist 종목과 완전히 동일한 계산식(compute_buy_score)을 그대로 적용할 뿐, 별도 로직은 없다.
    단, 대세 하락추세(역배열)인 종목은 지지선/PEG가 좋아 보여도 회복까지 오래 걸릴 수 있어 후보에서 제외한다."""
    universe = [t for t in get_nasdaq100_tickers() if t not in exclude_tickers]
    candidates = []
    for ticker in universe:
        try:
            q = get_quote(ticker)
            close_price = q.get("c")
            prev_close = q.get("pc")
            if not close_price:
                continue
            high_52w, low_52w, pe_ratio, peg_ratio = get_metrics(ticker)
            technical = compute_technical(ticker, q, str(report_date))
            if not technical:
                continue
            # 대세 하락추세(역배열)는 지지선/PEG가 좋아 보여도 회복까지 오래 걸릴 수 있어 후보에서 제외.
            if technical.get("ma_alignment") == "역배열(하락추세)":
                continue
            score = compute_buy_score(
                close_price,
                technical.get("swing_high"),
                technical.get("swing_low"),
                technical.get("sma120"),
                technical.get("sma200"),
                low_52w,
                peg_ratio,
                technical.get("ma_alignment"),
                technical.get("cross_signal"),
            )
            candidates.append(
                {
                    "market": "US",
                    "ticker": ticker,
                    "close_price": close_price,
                    "change_pct": (close_price - prev_close) / prev_close * 100 if prev_close else None,
                    "pe_ratio": pe_ratio,
                    "peg_ratio": peg_ratio,
                    **score,
                }
            )
        except Exception:
            continue

    candidates.sort(key=lambda c: c.get("buy_score") if c.get("buy_score") is not None else -1, reverse=True)
    top = candidates[:limit]

    # 상위 종목만 회사명(한국어 번역)/애널리스트 컨센서스를 추가로 채운다 (호출량 절약).
    for c in top:
        try:
            rating, _, _, _ = get_analyst_data(c["ticker"])
            c["analyst_rating"] = rating
        except Exception:
            c["analyst_rating"] = None
        try:
            name_en = _finnhub_get("stock/profile2", {"symbol": c["ticker"]}).json().get("name")
            c["display_name"] = translate_to_ko(name_en) if name_en else None
        except Exception:
            c["display_name"] = None

    return top


# 2026-08-27 기준 스냅샷(코스피 시가총액 상위, ETF 제외) — 네이버 실시간 조회가 실패할 때만 쓰는 비상용 대체 리스트.
_KOSPI100_FALLBACK = {
    "005930": "삼성전자", "000660": "SK하이닉스", "005935": "삼성전자우", "402340": "SK스퀘어",
    "009150": "삼성전기", "373220": "LG에너지솔루션", "005380": "현대차", "207940": "삼성바이오로직스",
    "028260": "삼성물산", "032830": "삼성생명", "105560": "KB금융", "012450": "한화에어로스페이스",
    "034020": "두산에너빌리티", "055550": "신한지주", "000270": "기아", "329180": "HD현대중공업",
    "006400": "삼성SDI", "068270": "셀트리온", "034730": "SK", "012330": "현대모비스",
    "086790": "하나금융지주", "035420": "NAVER", "066570": "LG전자", "010120": "LS ELECTRIC",
    "000810": "삼성화재", "010130": "고려아연", "267260": "HD현대일렉트릭", "298040": "효성중공업",
    "042660": "한화오션", "005490": "POSCO홀딩스", "009540": "HD한국조선해양", "316140": "우리금융지주",
    "015760": "한국전력", "017670": "SK텔레콤", "042700": "한미반도체", "011200": "HMM",
    "138040": "메리츠금융지주", "006800": "미래에셋증권", "051910": "LG화학", "000150": "두산",
    "096770": "SK이노베이션", "010140": "삼성중공업", "033780": "KT&G", "018260": "삼성에스디에스",
    "003550": "LG", "267250": "HD현대", "024110": "기업은행", "003670": "포스코퓨처엠",
    "278470": "에이피알", "079550": "LIG디펜스앤에어로스페이스", "035720": "카카오", "086280": "현대글로비스",
    "064350": "현대로템", "010950": "S-Oil", "000720": "현대건설", "272210": "한화시스템",
    "011070": "LG이노텍", "030200": "KT", "047810": "한국항공우주", "005830": "DB손해보험",
    "078930": "GS", "003230": "삼양식품", "307950": "현대오토에버", "071050": "한국금융지주",
    "003490": "대한항공", "323410": "카카오뱅크", "259960": "크래프톤", "443060": "HD현대마린솔루션",
    "028050": "삼성E&A", "005940": "NH투자증권", "006260": "LS", "047050": "포스코인터내셔널",
    "180640": "한진칼", "090430": "아모레퍼시픽", "047040": "대우건설", "161390": "한국타이어앤테크놀로지",
    "007660": "이수페타시스", "016360": "삼성증권", "352820": "하이브", "009830": "한화솔루션",
    "039490": "키움증권", "064400": "LG씨엔에스", "005387": "현대차2우B", "021240": "코웨이",
    "326030": "SK바이오팜", "000880": "한화", "267270": "HD건설기계", "000100": "유한양행",
    "128940": "한미약품",
}

_KR_ETF_NAME_MARKERS = (
    "KODEX", "TIGER", "KBSTAR", "ARIRANG", "SOL", "ACE", "HANARO", "KOSEF", "KINDEX", "TIMEFOLIO", "WOORI", "파워", "마이다스",
)


def get_kospi100_tickers():
    """네이버 금융 코스피 시가총액 순위 1~2페이지(상위 약 100개)를 실시간으로 가져온다 (키 불필요).
    ETF는 이름 패턴으로 걸러내 개별 종목만 남긴다. 실패하면 마지막 확인된 스냅샷으로 대체.
    반환값은 {6자리 종목코드: 한글 종목명} 딕셔너리."""
    try:
        rows = {}
        for page in (1, 2):
            r = requests.get(
                "https://finance.naver.com/sise/sise_market_sum.naver",
                headers={"User-Agent": "Mozilla/5.0"},
                params={"sosok": 0, "page": page},
                timeout=15,
            )
            r.raise_for_status()
            r.encoding = "euc-kr"
            for code, name in re.findall(r'/item/main\.naver\?code=(\d{6})[^>]*>([^<]+)</a>', r.text):
                rows[code] = name
        rows = {c: n for c, n in rows.items() if not any(m in n.upper() for m in _KR_ETF_NAME_MARKERS)}
        return rows or _KOSPI100_FALLBACK
    except Exception:
        return _KOSPI100_FALLBACK


def scan_kr_top_picks(exclude_tickers, report_date, limit=TOP_PICKS_COUNT):
    """코스피 시가총액 상위 종목(KODEX 100류 대형주 풀) 중 매수 근접도가 가장 높은 상위 N개를 찾는다.
    Finnhub가 한국 시장 시세를 무료 플랜에서 지원하지 않아(quote/metric 모두 빈 값), 이 스캔은
    Yahoo Finance 데이터만으로 compute_buy_score를 그대로 적용한다 — PEG는 구할 수 없어 중립 처리되고,
    나머지(지지선/눌림목/추세)는 미국 종목과 완전히 동일한 계산식이다."""
    universe = {code: name for code, name in get_kospi100_tickers().items() if code not in exclude_tickers}
    candidates = []
    for code, name in universe.items():
        ticker = f"{code}.KS"
        try:
            meta, history = get_yahoo_chart(ticker)
            if not history or len(history) < 2:
                continue
            close_price = meta.get("regularMarketPrice") or history[-1]["close"]
            prev_close = history[-2]["close"]
            high_52w = meta.get("fiftyTwoWeekHigh")
            low_52w = meta.get("fiftyTwoWeekLow")
            quote_like = {"c": close_price, "h": meta.get("regularMarketDayHigh"), "l": meta.get("regularMarketDayLow")}
            technical = compute_technical(ticker, quote_like, str(report_date), history=history)
            if not technical:
                continue
            if technical.get("ma_alignment") == "역배열(하락추세)":
                continue
            score = compute_buy_score(
                close_price,
                technical.get("swing_high"),
                technical.get("swing_low"),
                technical.get("sma120"),
                technical.get("sma200"),
                low_52w,
                None,  # PEG 데이터 없음 -> 중립 처리(compute_buy_score가 알아서 처리)
                technical.get("ma_alignment"),
                technical.get("cross_signal"),
            )
            candidates.append(
                {
                    "market": "KR",
                    "ticker": ticker,
                    "display_name": name,
                    "close_price": close_price,
                    "change_pct": (close_price - prev_close) / prev_close * 100 if prev_close else None,
                    "pe_ratio": None,
                    "peg_ratio": None,
                    "analyst_rating": None,
                    **score,
                }
            )
        except Exception:
            continue

    candidates.sort(key=lambda c: c.get("buy_score") if c.get("buy_score") is not None else -1, reverse=True)
    return candidates[:limit]


_last_finnhub_call = 0.0


def _finnhub_get(path, params):
    """무료 티어 분당 60콜 제한을 넘지 않도록 호출 사이 최소 간격을 보장하는 공통 래퍼."""
    global _last_finnhub_call
    elapsed = time.time() - _last_finnhub_call
    if elapsed < FINNHUB_CALL_DELAY_SEC:
        time.sleep(FINNHUB_CALL_DELAY_SEC - elapsed)
    r = requests.get(
        f"https://finnhub.io/api/v1/{path}",
        params={**params, "token": FINNHUB_API_KEY},
        timeout=15,
    )
    _last_finnhub_call = time.time()
    r.raise_for_status()
    return r


def get_quote(ticker):
    return _finnhub_get("quote", {"symbol": ticker}).json()  # c, h, l, o, pc, t


def get_metrics(ticker):
    """52주 고가/저가 + PER(TTM) + PEG(성장률 대비 PER, forward 우선 없으면 TTM).
    PEG는 '적정 PER'을 사람이 정하지 않아도 되게 하는 객관적 성장 대비 밸류에이션 지표."""
    try:
        m = _finnhub_get("stock/metric", {"symbol": ticker, "metric": "all"}).json().get("metric", {})
        pe = None
        for field in ("peTTM", "peBasicExclExtraTTM", "peExclExtraTTM", "peNormalizedAnnual"):
            if m.get(field):
                pe = m[field]
                break
        peg = m.get("forwardPEG") or m.get("pegTTM")
        return m.get("52WeekHigh"), m.get("52WeekLow"), pe, peg
    except Exception:
        return None, None, None, None


def get_analyst_data(ticker):
    """애널리스트 매수/보유/매도 컨센서스와 목표주가(평균/최고/최저)."""
    rating = None
    try:
        recs = _finnhub_get("stock/recommendation", {"symbol": ticker}).json()
        if recs:
            latest = recs[0]
            buy = latest.get("strongBuy", 0) + latest.get("buy", 0)
            hold = latest.get("hold", 0)
            sell = latest.get("sell", 0) + latest.get("strongSell", 0)
            rating = f"매수{buy}/보유{hold}/매도{sell}"
    except Exception:
        pass

    target_avg = target_high = target_low = None
    try:
        pt = _finnhub_get("stock/price-target", {"symbol": ticker}).json()
        target_avg = pt.get("targetMean")
        target_high = pt.get("targetHigh")
        target_low = pt.get("targetLow")
    except Exception:
        pass

    return rating, target_avg, target_high, target_low


def _is_relevant_headline(headline, ticker, display_name):
    """Finnhub의 company-news는 related 필드가 조회한 티커를 무조건 붙여주기만 해서
    (느슨한 태깅) 실제로는 관련 없는 기사도 섞여 나온다. 헤드라인에 티커나 회사명이
    실제로 등장하는지로 다시 걸러낸다."""
    text = headline.lower()
    candidates = [ticker.lstrip("^").lower()]
    if display_name:
        # "Company Inc." 같은 접미사 없이 핵심 이름만 비교 (예: "NVIDIA Corp" -> "nvidia")
        core = display_name.split()[0].lower()
        if len(core) >= 3:
            candidates.append(core)
    return any(c in text for c in candidates if c)


def translate_to_ko(text):
    """영어 헤드라인을 한국어로 번역 (무료 MyMemory API, 키 불필요).
    실패하면 원문을 그대로 반환 — 번역 실패가 리포트 생성을 막지 않도록."""
    if not text:
        return text
    try:
        r = requests.get(
            "https://api.mymemory.translated.net/get",
            params={"q": text, "langpair": "en|ko"},
            timeout=10,
        )
        r.raise_for_status()
        translated = r.json().get("responseData", {}).get("translatedText")
        return translated or text
    except Exception:
        return text


def get_company_news(ticker, display_name=None, days=3, limit=3):
    """등락률이 큰(이벤트가 있었던) 종목에 대해 최근 뉴스 헤드라인을 가져와 한국어로 번역한다.
    적정가 판단에 참고할 수 있도록 헤드라인/출처/링크만 간단히 담는다."""
    try:
        today = datetime.now(ZoneInfo("America/New_York")).date()
        frm = today - timedelta(days=days)
        articles = _finnhub_get("company-news", {"symbol": ticker, "from": str(frm), "to": str(today)}).json() or []
        articles.sort(key=lambda a: a.get("datetime", 0), reverse=True)
        articles = [a for a in articles if a.get("headline") and _is_relevant_headline(a["headline"], ticker, display_name)]
        return [
            {
                "headline": a.get("headline"),
                "headline_ko": translate_to_ko(a.get("headline")),
                "source": a.get("source"),
                "url": a.get("url"),
                "date": datetime.fromtimestamp(a["datetime"], tz=ZoneInfo("America/New_York")).date().isoformat()
                if a.get("datetime")
                else None,
            }
            for a in articles[:limit]
        ]
    except Exception:
        return []


def get_yahoo_chart(ticker, days=260):
    """Yahoo Finance 차트 API를 한 번 호출해서 meta(현재가/52주 고저 등)와 일봉 히스토리를 같이 가져온다
    (키 불필요, 무료). 한국 주식처럼 Finnhub가 커버하지 않는 시장도 이걸로 충당한다."""
    try:
        r = requests.get(
            f"https://query1.finance.yahoo.com/v8/finance/chart/{ticker}",
            headers={"User-Agent": "Mozilla/5.0"},
            params={"range": "1y", "interval": "1d"},
            timeout=15,
        )
        r.raise_for_status()
        result = r.json()["chart"]["result"][0]
        meta = result.get("meta", {})
        timestamps = result["timestamp"]
        q = result["indicators"]["quote"][0]
        closes, highs, lows = q["close"], q["high"], q["low"]
        history = []
        for ts, c, h, l in zip(timestamps, closes, highs, lows):
            if c is None:
                continue
            date_str = datetime.fromtimestamp(ts, tz=ZoneInfo("America/New_York")).date().isoformat()
            history.append(
                {"date": date_str, "close": float(c), "high": float(h if h is not None else c), "low": float(l if l is not None else c)}
            )
        return meta, history[-days:]
    except Exception:
        return {}, []


def get_price_history(ticker, days=260):
    """[{"date", "close", "high", "low"}, ...] 오름차순. (Stooq는 봇 차단 챌린지가 걸려 있어 사용 불가)"""
    return get_yahoo_chart(ticker, days)[1]


def _find_recent_swing(history, window=5):
    """가장 최근 확정된 스윙 고점/저점(직전 고점/저점)을 프랙탈 방식으로 찾는다.
    좌우 window일 동안 각각 최고가/최저가인 지점을 최근 순으로 탐색."""
    n = len(history)
    swing_high = swing_low = None
    for i in range(n - 1 - window, window - 1, -1):
        seg = history[i - window : i + window + 1]
        if swing_high is None and history[i]["high"] == max(p["high"] for p in seg):
            swing_high = (history[i]["date"], history[i]["high"])
        if swing_low is None and history[i]["low"] == min(p["low"] for p in seg):
            swing_low = (history[i]["date"], history[i]["low"])
        if swing_high and swing_low:
            break
    return swing_high, swing_low


def _sma(values, window):
    if len(values) < window:
        return None
    return sum(values[-window:]) / window


def _rolling_sma(values, window):
    return [_sma(values[: i + 1], window) for i in range(len(values))]


def _find_recent_cross(closes, short_w, long_w, lookback=10):
    """최근 lookback 거래일 내 골든/데드크로스가 있었으면 (며칠전, 종류) 반환."""
    short_series = _rolling_sma(closes, short_w)
    long_series = _rolling_sma(closes, long_w)
    n = len(closes)
    last_event = None
    for i in range(max(1, n - lookback), n):
        s_now, l_now, s_prev, l_prev = short_series[i], long_series[i], short_series[i - 1], long_series[i - 1]
        if None in (s_now, l_now, s_prev, l_prev):
            continue
        if s_prev <= l_prev and s_now > l_now:
            last_event = (n - 1 - i, "골든크로스")
        elif s_prev >= l_prev and s_now < l_now:
            last_event = (n - 1 - i, "데드크로스")
    return last_event


def compute_technical(ticker, latest_quote, latest_date, history=None):
    """20/60/120/200일 SMA, 정배열/역배열, 최근 골든/데드크로스, 직전 스윙 고점/저점을 계산한다.
    history를 미리 받으면(예: get_yahoo_chart로 한 번에 가져온 경우) 중복 API 호출을 피한다."""
    if history is None:
        history = get_price_history(ticker)
    if not history:
        return {}

    latest_price = latest_quote.get("c") if latest_quote else None
    latest_high = latest_quote.get("h") if latest_quote else None
    latest_low = latest_quote.get("l") if latest_quote else None

    # Yahoo 마지막 거래일이 오늘 데이터를 아직 반영 안 했으면 Finnhub 현재가를 오늘자로 덧붙인다.
    if latest_price:
        today_point = {
            "date": latest_date,
            "close": latest_price,
            "high": latest_high or latest_price,
            "low": latest_low or latest_price,
        }
        if history and history[-1]["date"] == latest_date:
            history[-1] = today_point
        else:
            history.append(today_point)

    closes = [p["close"] for p in history]
    swing_high, swing_low = _find_recent_swing(history)

    sma20, sma60, sma120, sma200 = _sma(closes, 20), _sma(closes, 60), _sma(closes, 120), _sma(closes, 200)

    alignment = None
    if latest_price and sma20 and sma60 and sma120 and sma200:
        if latest_price > sma20 > sma60 > sma120 > sma200:
            alignment = "정배열(상승추세)"
        elif latest_price < sma20 < sma60 < sma120 < sma200:
            alignment = "역배열(하락추세)"
        else:
            alignment = "혼조"

    signals = []
    for short_w, long_w, label in ((20, 60, "20/60일선"), (60, 120, "60/120일선"), (120, 200, "120/200일선")):
        event = _find_recent_cross(closes, short_w, long_w)
        if event:
            days_ago, kind = event
            when = "오늘" if days_ago == 0 else f"{days_ago}일전"
            signals.append(f"{label} {kind}({when})")

    return {
        "sma20": round(sma20, 2) if sma20 else None,
        "sma60": round(sma60, 2) if sma60 else None,
        "sma120": round(sma120, 2) if sma120 else None,
        "sma200": round(sma200, 2) if sma200 else None,
        "ma_alignment": alignment,
        "cross_signal": ", ".join(signals) if signals else "크로스 없음",
        "swing_high": round(swing_high[1], 2) if swing_high else None,
        "swing_high_date": swing_high[0] if swing_high else None,
        "swing_low": round(swing_low[1], 2) if swing_low else None,
        "swing_low_date": swing_low[0] if swing_low else None,
    }


def _support_score(close_price, support_low):
    """지지선 근접도(0~40점): 계산 가능한 지지선 중 가장 낮은 값에 얼마나 가까운지.
    이미 그 지지선 아래로 왔으면 만점, 20% 이상 위에 있으면 0점, 사이는 선형 보간."""
    if not close_price or not support_low:
        return 0.0
    gap_pct = (close_price - support_low) / support_low * 100
    if gap_pct <= 0:
        return 40.0
    if gap_pct >= 20:
        return 0.0
    return 40.0 * (1 - gap_pct / 20)


def _pullback_score(close_price, swing_high):
    """눌림목 깊이(0~25점): 직전 고점 대비 얼마나 빠졌는지. 많이 빠질수록 높은 점수."""
    if not close_price or not swing_high:
        return 0.0
    pullback_pct = (swing_high - close_price) / swing_high * 100
    if pullback_pct <= 0:
        return 0.0
    if pullback_pct >= 30:
        return 25.0
    return 25.0 * (pullback_pct / 30)


def _peg_score(peg):
    """성장 대비 밸류에이션(0~20점): PEG가 낮을수록(성장 대비 저렴할수록) 높은 점수.
    PEG를 못 구하거나 음수(적자/역성장이라 왜곡)면 중립값(10점)으로 처리."""
    if not peg or peg <= 0:
        return 10.0
    if peg <= 1:
        return 20.0
    if peg >= 3:
        return 0.0
    return 20.0 * (1 - (peg - 1) / 2)


def _trend_score(ma_alignment, cross_signal):
    """추세 구조(0~15점): 정배열/골든크로스면 가산, 역배열/데드크로스면 감산, 그 외 중립."""
    score = 7.5
    if ma_alignment == "정배열(상승추세)":
        score += 7.5
    elif ma_alignment == "역배열(하락추세)":
        score -= 7.5

    if cross_signal:
        if "골든크로스" in cross_signal and ("오늘" in cross_signal or "일전" in cross_signal):
            score += 3
        elif "데드크로스" in cross_signal:
            score -= 3
    return max(0.0, min(15.0, score))


def compute_buy_score(close_price, swing_high, swing_low, sma120, sma200, low_52w, peg, ma_alignment, cross_signal):
    """지지선 근접도 + 눌림목 깊이 + PEG 밸류에이션 + 추세 구조를 합친 0~100점 매수 근접도 점수.
    모든 종목에 동일하게 적용되는 계산이며, 사람이 입력하는 값은 없다.
    (기계적 계산일 뿐 투자 조언이 아니며 최종 판단은 본인 몫)"""
    support_candidates = {
        "직전 스윙저점": swing_low,
        "120일선": sma120,
        "200일선": sma200,
        "52주 저가": low_52w,
    }
    support_candidates = {k: v for k, v in support_candidates.items() if v}

    support_low_label = support_low = None
    if support_candidates and close_price:
        # 현재가 바로 아래(또는 근처)에서 가장 가까운 지지선을 기준으로 삼는다.
        # 52주 저가처럼 너무 먼 지지선이 기준이 되지 않도록, 가격 밑에 있는 후보 중 "가장 높은"(=가장 가까운) 것을 선택.
        below = {k: v for k, v in support_candidates.items() if v <= close_price}
        if below:
            support_low_label, support_low = max(below.items(), key=lambda kv: kv[1])
        else:
            # 현재가가 모든 지지선보다 이미 낮음 = 최대 지지 상태
            support_low_label, support_low = min(support_candidates.items(), key=lambda kv: kv[1])

    s_support = _support_score(close_price, support_low)
    s_pullback = _pullback_score(close_price, swing_high)
    s_peg = _peg_score(peg)
    s_trend = _trend_score(ma_alignment, cross_signal)
    total = round(s_support + s_pullback + s_peg + s_trend, 1)

    if total >= 65:
        label = "매수 근접(강한 신호)"
    elif total >= 40:
        label = "관망(접근 중)"
    else:
        label = "아직 매수권 아님"

    detail = (
        f"지지선{s_support:.0f}/40({support_low_label or '-'} {support_low or '-'}) · "
        f"눌림목{s_pullback:.0f}/25 · PEG{s_peg:.0f}/20({peg if peg else '-'}) · 추세{s_trend:.0f}/15"
    )

    return {
        "buy_score": total,
        "buy_score_label": label,
        "buy_score_detail": detail,
        "nearest_support": round(support_low, 2) if support_low else None,
        "nearest_support_label": support_low_label,
    }


def _resistance_score(close_price, resistance_high):
    """저항선 근접도(0~40점): 계산 가능한 저항선 중 가장 가까운 값에 얼마나 근접했는지.
    이미 그 저항선을 넘었으면 만점, 20% 이상 아래에 있으면 0점, 사이는 선형 보간."""
    if not close_price or not resistance_high:
        return 0.0
    gap_pct = (resistance_high - close_price) / close_price * 100
    if gap_pct <= 0:
        return 40.0
    if gap_pct >= 20:
        return 0.0
    return 40.0 * (1 - gap_pct / 20)


def _rally_score(close_price, swing_low):
    """급등 과열도(0~25점): 직전 저점 대비 얼마나 올랐는지. 많이 오를수록 높은 점수(눌림목 깊이의 반대)."""
    if not close_price or not swing_low:
        return 0.0
    rally_pct = (close_price - swing_low) / swing_low * 100
    if rally_pct <= 0:
        return 0.0
    if rally_pct >= 30:
        return 25.0
    return 25.0 * (rally_pct / 30)


def _peg_sell_score(peg):
    """성장 대비 밸류에이션(0~20점): PEG가 높을수록(성장 대비 비쌀수록) 높은 점수. PEG 점수의 반대."""
    if not peg or peg <= 0:
        return 10.0
    if peg >= 3:
        return 20.0
    if peg <= 1:
        return 0.0
    return 20.0 * ((peg - 1) / 2)


def _trend_sell_score(ma_alignment, cross_signal):
    """추세 구조(0~15점): 역배열/데드크로스면 가산, 정배열/골든크로스면 감산. 추세 점수의 반대."""
    score = 7.5
    if ma_alignment == "역배열(하락추세)":
        score += 7.5
    elif ma_alignment == "정배열(상승추세)":
        score -= 7.5

    if cross_signal:
        if "데드크로스" in cross_signal and ("오늘" in cross_signal or "일전" in cross_signal):
            score += 3
        elif "골든크로스" in cross_signal:
            score -= 3
    return max(0.0, min(15.0, score))


def compute_sell_score(close_price, swing_high, swing_low, sma120, sma200, high_52w, peg, ma_alignment, cross_signal):
    """저항선 근접도 + 급등 과열도 + PEG 밸류에이션(비쌈) + 추세 구조를 합친 0~100점 매도 근접도 점수.
    compute_buy_score와 완전히 대칭되는 계산이며 모든 종목에 동일하게 적용된다.
    (기계적 계산일 뿐 투자 조언이 아니며 최종 판단은 본인 몫)"""
    resistance_candidates = {
        "직전 스윙고점": swing_high,
        "120일선": sma120,
        "200일선": sma200,
        "52주 고가": high_52w,
    }
    resistance_candidates = {k: v for k, v in resistance_candidates.items() if v}

    resistance_high_label = resistance_high = None
    if resistance_candidates and close_price:
        # 현재가 바로 위(또는 근처)에서 가장 가까운 저항선을 기준으로 삼는다.
        above = {k: v for k, v in resistance_candidates.items() if v >= close_price}
        if above:
            resistance_high_label, resistance_high = min(above.items(), key=lambda kv: kv[1])
        # 모든 저항선을 이미 넘었으면(above가 비었으면) 근접도는 0점 처리 — 상단 과열은 rally 점수가 대신 반영

    s_resistance = _resistance_score(close_price, resistance_high)
    s_rally = _rally_score(close_price, swing_low)
    s_peg = _peg_sell_score(peg)
    s_trend = _trend_sell_score(ma_alignment, cross_signal)
    total = round(s_resistance + s_rally + s_peg + s_trend, 1)

    if total >= 65:
        label = "매도 근접(강한 신호)"
    elif total >= 40:
        label = "관망(경계)"
    else:
        label = "아직 매도권 아님"

    detail = (
        f"저항선{s_resistance:.0f}/40({resistance_high_label or '-'} {resistance_high or '-'}) · "
        f"과열도{s_rally:.0f}/25 · PEG{s_peg:.0f}/20({peg if peg else '-'}) · 추세{s_trend:.0f}/15"
    )

    return {
        "sell_score": total,
        "sell_score_label": label,
        "sell_score_detail": detail,
        "nearest_resistance": round(resistance_high, 2) if resistance_high else None,
        "nearest_resistance_label": resistance_high_label,
    }


def us_trading_date():
    # 스크립트는 미국장 마감 이후(뉴욕 시간 기준 당일 저녁)에 실행되므로
    # 뉴욕 기준 오늘 날짜가 곧 이 리포트가 다루는 거래일.
    return datetime.now(ZoneInfo("America/New_York")).date()


def upsert_report(report_date):
    existing = sb_get("daily_reports", params={"report_date": f"eq.{report_date}", "select": "id"})
    if existing:
        return existing[0]["id"]
    created = sb_post("daily_reports", {"report_date": str(report_date)})
    return created[0]["id"]


def save_items(report_id, items):
    # 재실행 대비: 기존 항목 삭제 후 재삽입
    requests.delete(
        f"{SUPABASE_URL}/rest/v1/report_items",
        headers=SB_HEADERS,
        params={"report_id": f"eq.{report_id}"},
        timeout=30,
    )
    rows = [{"report_id": report_id, **item} for item in items]
    if rows:
        sb_post("report_items", rows)


def save_top_picks(report_id, picks, market):
    # market별로만 삭제 — US/KR 두 스캔이 같은 daily_picks 테이블을 나눠 쓰므로 서로를 지우면 안 된다.
    requests.delete(
        f"{SUPABASE_URL}/rest/v1/daily_picks",
        headers=SB_HEADERS,
        params={"report_id": f"eq.{report_id}", "market": f"eq.{market}"},
        timeout=30,
    )
    rows = [{"report_id": report_id, "rank": i + 1, **p} for i, p in enumerate(picks)]
    if rows:
        sb_post("daily_picks", rows)


def log_send(report_id, status, detail):
    sb_post("send_log", {"report_id": report_id, "channel": "email", "status": status, "detail": detail})


def send_email(report_date, items):
    link = f"{REPORT_BASE_URL}/report.html?date={report_date}"
    lines = []
    for it in items:
        if it.get("change_pct") is None:
            continue
        pe = f", PER {it['pe_ratio']:.1f}" if it.get("pe_ratio") else ""
        peg = f", PEG {it['peg_ratio']:.2f}" if it.get("peg_ratio") else ""
        cross = f", {it['cross_signal']}" if it.get("cross_signal") and it["cross_signal"] != "크로스 없음" else ""
        rating = f", {it['analyst_rating']}" if it.get("analyst_rating") else ""
        buy_score = it.get("buy_score")
        sell_score = it.get("sell_score")
        buy_txt = f", 매수근접도 {buy_score:.0f}점({it.get('buy_score_label')})" if buy_score is not None else ""
        sell_txt = f", 매도근접도 {sell_score:.0f}점({it.get('sell_score_label')})" if sell_score is not None else ""
        news_txt = f", 📰뉴스 {len(it['news'])}건" if it.get("news") else ""
        pnl_txt = f", 보유 {it['quantity']}주 손익 {it['pnl_pct']:+.1f}%(${it['pnl_abs']:+.0f})" if it.get("pnl_pct") is not None else ""
        buy_icon = "🔥" if buy_score is not None and buy_score >= 65 else "🎯" if buy_score is not None and buy_score >= 40 else ""
        sell_icon = "⚠️" if sell_score is not None and sell_score >= 65 else "🔻" if sell_score is not None and sell_score >= 40 else ""
        icon = f"{buy_icon}{sell_icon} " if buy_icon or sell_icon else ""
        lines.append(
            f"{icon}{it['ticker']}: {it['close_price']} ({it['change_pct']:+.2f}%){pe}{peg}{cross}{buy_txt}{sell_txt}{pnl_txt}{rating}{news_txt}"
        )

    holding_items = [it for it in items if it.get("pnl_abs") is not None]
    portfolio_line = ""
    if holding_items:
        total_pnl = sum(it["pnl_abs"] for it in holding_items)
        portfolio_line = f"\n\n보유종목 합산 평가손익: ${total_pnl:+,.0f} ({len(holding_items)}종목)"

    body = (
        f"{report_date} 미국주식 관심종목 리포트가 도착했습니다.\n\n"
        + "\n".join(lines)
        + portfolio_line
        + f"\n\n전체 리포트 보기: {link}"
    )
    strong_hits = [it["ticker"] for it in items if it.get("buy_score") is not None and it["buy_score"] >= 65]
    watch_hits = [
        it["ticker"]
        for it in items
        if it.get("buy_score") is not None and 40 <= it["buy_score"] < 65 and it["ticker"] not in strong_hits
    ]
    sell_strong_hits = [it["ticker"] for it in items if it.get("sell_score") is not None and it["sell_score"] >= 65]
    breakouts = [it["ticker"] for it in items if it.get("cross_signal") and "골든크로스(오늘)" in it["cross_signal"]]
    event_tickers = [it["ticker"] for it in items if it.get("news")]
    prefix_parts = []
    if strong_hits:
        prefix_parts.append(f"🔥 매수 근접(강): {', '.join(strong_hits)}")
    if watch_hits:
        prefix_parts.append(f"🎯 매수 근접(관망): {', '.join(watch_hits)}")
    if sell_strong_hits:
        prefix_parts.append(f"⚠️ 매도 근접(강): {', '.join(sell_strong_hits)}")
    if breakouts:
        prefix_parts.append(f"🚀 골든크로스 발생: {', '.join(breakouts)}")
    if event_tickers:
        prefix_parts.append(f"📰 이벤트: {', '.join(event_tickers)}")
    subject_prefix = f"[{' / '.join(prefix_parts)}] " if prefix_parts else ""
    msg = MIMEText(body)
    msg["Subject"] = f"{subject_prefix}[주식 리포트] {report_date} 관심종목 요약"
    msg["From"] = GMAIL_USER
    msg["To"] = RECIPIENT_EMAIL

    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
        server.login(GMAIL_USER, GMAIL_APP_PASSWORD)
        server.sendmail(GMAIL_USER, [RECIPIENT_EMAIL], msg.as_string())


def main():
    report_date = us_trading_date()
    watchlist = get_watchlist()
    holdings = get_holdings()
    watchlist = merge_watchlist_and_holdings(watchlist, holdings)
    if not watchlist:
        print("watchlist/holdings가 비어 있습니다. Supabase 테이블을 확인하세요.")
        sys.exit(0)

    items = []
    for row in watchlist:
        ticker = row["ticker"]
        notes = []

        # Finnhub 쪽(현재가/PER/애널리스트)은 키가 없거나 실패해도 Yahoo 기반
        # 이동평균선/크로스/스윙 계산은 그대로 진행되도록 각각 독립적으로 처리.
        q = {}
        try:
            q = get_quote(ticker)
        except Exception as e:
            notes.append(f"시세 조회 실패: {e}")

        try:
            high_52w, low_52w, pe_ratio, peg_ratio = get_metrics(ticker)
        except Exception:
            high_52w = low_52w = pe_ratio = peg_ratio = None

        try:
            analyst_rating, target_avg, target_high, target_low = get_analyst_data(ticker)
        except Exception:
            analyst_rating = target_avg = target_high = target_low = None

        close_price = q.get("c")
        prev_close = q.get("pc")
        change_pct = None
        if close_price and prev_close:
            change_pct = (close_price - prev_close) / prev_close * 100

        technical = compute_technical(ticker, q, str(report_date))
        if not technical:
            notes.append("일봉 히스토리 조회 실패")

        # ^TNX 같은 지수/금리 티커는 PEG·EPS 개념이 없어 매수/매도 근접도 점수가 무의미하므로 제외.
        is_equity = not ticker.startswith("^")
        if is_equity:
            buy_score = compute_buy_score(
                close_price,
                technical.get("swing_high"),
                technical.get("swing_low"),
                technical.get("sma120"),
                technical.get("sma200"),
                low_52w,
                peg_ratio,
                technical.get("ma_alignment"),
                technical.get("cross_signal"),
            )
            sell_score = compute_sell_score(
                close_price,
                technical.get("swing_high"),
                technical.get("swing_low"),
                technical.get("sma120"),
                technical.get("sma200"),
                high_52w,
                peg_ratio,
                technical.get("ma_alignment"),
                technical.get("cross_signal"),
            )
        else:
            buy_score = {
                "buy_score": None,
                "buy_score_label": None,
                "buy_score_detail": "지수/금리 티커는 매수 근접도 계산 대상 아님",
                "nearest_support": None,
                "nearest_support_label": None,
            }
            sell_score = {
                "sell_score": None,
                "sell_score_label": None,
                "sell_score_detail": "지수/금리 티커는 매도 근접도 계산 대상 아님",
                "nearest_resistance": None,
                "nearest_resistance_label": None,
            }
        score = {**buy_score, **sell_score}

        # 등락률이 큰(이벤트가 있었던) 종목만 뉴스 첨부 — 모든 종목에 공통 적용되는 규칙
        news = []
        if change_pct is not None and abs(change_pct) >= EVENT_MOVE_THRESHOLD_PCT:
            news = get_company_news(ticker, row.get("display_name"))

        # 보유종목이면 평단가 대비 수익률 계산 (관심종목 전용이면 quantity/avg_buy_price가 None)
        quantity = row.get("quantity")
        avg_buy_price = row.get("avg_buy_price")
        pnl_pct = pnl_abs = None
        if quantity and avg_buy_price and close_price:
            pnl_pct = (close_price - avg_buy_price) / avg_buy_price * 100
            pnl_abs = (close_price - avg_buy_price) * quantity

        items.append(
            {
                "ticker": ticker,
                "display_name": row.get("display_name"),
                "close_price": close_price,
                "prev_close": prev_close,
                "change_pct": change_pct,
                "high_52w": high_52w,
                "low_52w": low_52w,
                "pe_ratio": pe_ratio,
                "peg_ratio": peg_ratio,
                "thesis": row.get("thesis"),
                "analyst_rating": analyst_rating,
                "target_price_avg": target_avg,
                "target_price_high": target_high,
                "target_price_low": target_low,
                "news": news or None,
                "quantity": quantity,
                "avg_buy_price": avg_buy_price,
                "pnl_pct": pnl_pct,
                "pnl_abs": round(pnl_abs, 2) if pnl_abs is not None else None,
                "note": "; ".join(notes) if notes else None,
                **technical,
                **score,
            }
        )

    report_id = upsert_report(report_date)
    save_items(report_id, items)

    tracked_tickers = {row["ticker"] for row in watchlist}
    top_picks = scan_top_picks(tracked_tickers, report_date)
    save_top_picks(report_id, top_picks, "US")

    kr_top_picks = scan_kr_top_picks(set(), report_date)
    save_top_picks(report_id, kr_top_picks, "KR")

    print(
        f"리포트 생성 완료: {report_date} ({len(items)}개 종목, 나스닥100 스캔 상위 {len(top_picks)}개, "
        f"코스피 스캔 상위 {len(kr_top_picks)}개) - {REPORT_BASE_URL}/report.html?date={report_date}"
    )

    if not EMAIL_ENABLED:
        print("GMAIL_USER/GMAIL_APP_PASSWORD 미설정 - 이메일 발송 없이 종료")
        return

    try:
        send_email(report_date, items)
        log_send(report_id, "success", f"{len(items)}개 종목 발송")
        print("이메일 발송 완료")
    except Exception as e:
        log_send(report_id, "failed", str(e))
        print(f"이메일 발송 실패: {e}")
        raise


if __name__ == "__main__":
    main()
