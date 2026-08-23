-- Supabase 스키마: 미국주식 관심종목 일일 리포트
-- Supabase 대시보드 > SQL Editor 에서 이 파일 전체를 실행하세요.
--
-- 설계 원칙: watchlist에는 "이 종목을 본다"는 사실과 본인의 메모(thesis)만 있고,
-- 적정 PER/매수 목표가처럼 판단이 필요한 값은 전부 report_items에서 매일 자동 계산됩니다
-- (PEG·이동평균·지지선·눌림목을 조합한 "매수 근접도" 점수). 사람이 손으로 넣는 가격/배수는 없습니다.

create table if not exists watchlist (
  id bigint generated always as identity primary key,
  ticker text not null unique,
  display_name text,
  thesis text,  -- 본인 투자 논리/메모(자유 텍스트) — 계산에는 안 쓰이고 리포트에 참고용으로만 표시
  active boolean not null default true,
  created_at timestamptz not null default now()
);

alter table watchlist add column if not exists thesis text;

-- 예전 버전에서 쓰던 수동 입력 필드(적정PER/매수목표가)는 더 이상 사용하지 않음 — 있다면 제거.
alter table watchlist drop column if exists fair_pe;
alter table watchlist drop column if exists buy_target_price;
alter table watchlist drop column if exists strong_buy_target_price;

-- 실제 보유 종목 (관심종목과 별개 — 매수 평단가/수량 기록)
create table if not exists holdings (
  id bigint generated always as identity primary key,
  ticker text not null,
  quantity numeric not null,
  avg_buy_price numeric not null,
  buy_date date,
  note text,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);

create table if not exists daily_reports (
  id bigint generated always as identity primary key,
  report_date date not null unique, -- 리포트가 다루는 미국 거래일(America/New_York 기준)
  generated_at timestamptz not null default now(),
  summary text
);

create table if not exists report_items (
  id bigint generated always as identity primary key,
  report_id bigint not null references daily_reports(id) on delete cascade,
  ticker text not null,
  close_price numeric,
  prev_close numeric,
  change_pct numeric,
  high_52w numeric,
  low_52w numeric,
  pe_ratio numeric,       -- 트레일링 PER (참고용)
  peg_ratio numeric,      -- PER ÷ 이익성장률. "적정 PER"을 사람이 정하지 않아도 되는 객관적 성장 대비 밸류에이션
  sma20 numeric,
  sma60 numeric,
  sma120 numeric,
  sma200 numeric,
  ma_alignment text,   -- 정배열 / 역배열 / 혼조 (가격>20>60>120>200 순 기준)
  cross_signal text,   -- 예: "20/60일선 골든크로스(2일전)"
  swing_high numeric,      -- 직전 스윙 고점(프랙탈)
  swing_high_date date,
  swing_low numeric,       -- 직전 스윙 저점(프랙탈)
  swing_low_date date,
  nearest_support numeric,       -- 현재가 바로 아래 가장 가까운 지지선 (스윙저점/120일선/200일선/52주저가 중 자동 선택)
  nearest_support_label text,    -- 어떤 지지선이 선택됐는지
  buy_score numeric,             -- 지지선 근접도+눌림목 깊이+PEG+추세를 합친 0~100점 "매수 근접도" (공통 계산, 수동입력 없음)
  buy_score_label text,          -- 매수 근접(강한 신호) / 관망(접근 중) / 아직 매수권 아님
  buy_score_detail text,         -- 점수 구성 breakdown (투명성용)
  analyst_rating text,        -- 예: "매수18/보유5/매도1"
  target_price_avg numeric,   -- 애널리스트 목표주가 평균 (Finnhub 유료 플랜 필요, 무료 플랜은 비어있음)
  target_price_high numeric,
  target_price_low numeric,
  thesis text,      -- 당시 watchlist.thesis 스냅샷
  news jsonb,        -- 등락률이 큰(이벤트) 종목만 최근 뉴스 헤드라인 [{headline,source,url,date}, ...]
  quantity numeric,        -- 보유수량 (holdings에 있는 종목만, 관심종목 전용이면 null)
  avg_buy_price numeric,   -- 평단가 (holdings 스냅샷)
  pnl_pct numeric,         -- 평단가 대비 수익률(%)
  pnl_abs numeric,         -- 평단가 대비 평가손익($, quantity 반영)
  note text
);

-- 기존에 이미 테이블을 만든 경우를 위한 안전장치 (컬럼 추가/정리)
alter table report_items add column if not exists pe_ratio numeric;
alter table report_items add column if not exists peg_ratio numeric;
alter table report_items add column if not exists sma20 numeric;
alter table report_items add column if not exists sma60 numeric;
alter table report_items add column if not exists sma120 numeric;
alter table report_items add column if not exists sma200 numeric;
alter table report_items add column if not exists ma_alignment text;
alter table report_items add column if not exists cross_signal text;
alter table report_items add column if not exists swing_high numeric;
alter table report_items add column if not exists swing_high_date date;
alter table report_items add column if not exists swing_low numeric;
alter table report_items add column if not exists swing_low_date date;
alter table report_items add column if not exists nearest_support numeric;
alter table report_items add column if not exists nearest_support_label text;
alter table report_items add column if not exists buy_score numeric;
alter table report_items add column if not exists buy_score_label text;
alter table report_items add column if not exists buy_score_detail text;
alter table report_items add column if not exists analyst_rating text;
alter table report_items add column if not exists target_price_avg numeric;
alter table report_items add column if not exists target_price_high numeric;
alter table report_items add column if not exists target_price_low numeric;
alter table report_items add column if not exists thesis text;
alter table report_items add column if not exists news jsonb;
alter table report_items add column if not exists quantity numeric;
alter table report_items add column if not exists avg_buy_price numeric;
alter table report_items add column if not exists pnl_pct numeric;
alter table report_items add column if not exists pnl_abs numeric;

-- 예전 버전의 수동 판단 필드는 더 이상 사용하지 않음 — 있다면 제거.
alter table report_items drop column if exists fair_pe;
alter table report_items drop column if exists valuation_signal;
alter table report_items drop column if exists action_signal;
alter table report_items drop column if exists buy_target_price;
alter table report_items drop column if exists buy_target_source;
alter table report_items drop column if exists at_buy_target;
alter table report_items drop column if exists strong_buy_target_price;
alter table report_items drop column if exists at_strong_buy_target;

create table if not exists send_log (
  id bigint generated always as identity primary key,
  report_id bigint references daily_reports(id),
  channel text not null default 'email',
  status text not null, -- success / failed
  detail text,
  sent_at timestamptz not null default now()
);

-- 리포트 페이지(docs/*.html)가 anon key로 읽을 수 있도록 RLS 오픈.
-- watchlist / holdings 는 로그인(매직링크) 후에만 읽기/쓰기 가능 (아래 정책 참고).
-- send_log 는 아무 공개 정책도 없어 service role key로만 접근 가능.
alter table daily_reports enable row level security;
alter table report_items enable row level security;
alter table watchlist enable row level security;
alter table holdings enable row level security;
alter table send_log enable row level security;

drop policy if exists "public read daily_reports" on daily_reports;
create policy "public read daily_reports" on daily_reports
  for select using (true);

drop policy if exists "public read report_items" on report_items;
create policy "public read report_items" on report_items
  for select using (true);

-- 관리 앱(docs/manage.html)은 Supabase Auth 매직링크로 로그인한 사용자만 접근.
-- Authentication > Settings 에서 "Allow new user signups"를 꺼서 본인 계정 외에는
-- 신규가입 자체가 안 되게 막아야 안전합니다 (README 참고).
drop policy if exists "authenticated manage watchlist" on watchlist;
create policy "authenticated manage watchlist" on watchlist
  for all using (auth.role() = 'authenticated') with check (auth.role() = 'authenticated');

drop policy if exists "authenticated manage holdings" on holdings;
create policy "authenticated manage holdings" on holdings
  for all using (auth.role() = 'authenticated') with check (auth.role() = 'authenticated');

-- 초기 관심종목 예시 (원하는 대로 추가/삭제, thesis는 자유 메모)
insert into watchlist (ticker, display_name)
values ('CAT', 'Caterpillar'), ('NVDA', 'NVIDIA'), ('PLTR', 'Palantir')
on conflict (ticker) do nothing;
