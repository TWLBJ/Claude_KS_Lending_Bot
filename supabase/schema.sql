-- ═══════════════════════════════════════════════════════════════
-- Bitfinex 放貸機器人 Supabase Schema
-- 使用方式：Supabase Dashboard → SQL Editor → 貼上整份執行
-- ═══════════════════════════════════════════════════════════════

create extension if not exists pgcrypto;

-- ── 資料表 ──────────────────────────────────────────────

-- 每循環的市場快照
create table if not exists market_snapshots (
  id bigint generated always as identity primary key,
  ts timestamptz not null default now(),
  symbol text not null,
  frr double precision,
  best_ask double precision,
  depth_rate double precision,
  trade_iqm double precision,
  recent_high double precision,
  spike boolean default false,
  anchor double precision,
  anchor_apy double precision
);
create index if not exists idx_market_snapshots_ts on market_snapshots (ts desc);

-- 機器人動作紀錄（掛單/撤單/成交）
create table if not exists actions_log (
  id bigint generated always as identity primary key,
  ts timestamptz not null default now(),
  action text not null,
  detail jsonb
);
create index if not exists idx_actions_log_ts on actions_log (ts desc);

-- 放貸中部位快照
create table if not exists credits_snapshots (
  id bigint generated always as identity primary key,
  ts timestamptz not null default now(),
  symbol text not null,
  total_lent double precision,
  weighted_rate double precision,
  weighted_apy double precision,
  count int,
  details jsonb
);
create index if not exists idx_credits_snapshots_ts on credits_snapshots (ts desc);

-- 每日利息收益（從 Bitfinex ledger 同步，UTC+8 日期）
create table if not exists earnings (
  date date not null,
  currency text not null,
  amount double precision not null,
  balance double precision,
  primary key (date, currency)
);

-- 機器人最新狀態（單列，網頁總覽用）
create table if not exists bot_status (
  id int primary key,
  ts timestamptz,
  mode text,
  paused boolean,
  available double precision,
  total_lent double precision,
  weighted_apy double precision,
  credits_count int,
  offers_count int,
  offers jsonb,
  anchor_apy double precision,
  frr_apy double precision,
  spike boolean
);

-- 設定（dashboard token 的 sha256 雜湊）
create table if not exists app_settings (
  key text primary key,
  value text not null
);

-- ── 安全：全部開 RLS、不給 anon 任何 policy（= 拒絕直接存取）──
-- 機器人用 service_role key 寫入，會繞過 RLS。
alter table market_snapshots  enable row level security;
alter table actions_log       enable row level security;
alter table credits_snapshots enable row level security;
alter table earnings          enable row level security;
alter table bot_status        enable row level security;
alter table app_settings      enable row level security;

-- ── 設定你的 Dashboard 私人 token ──────────────────────────
-- ⚠️ 把 'CHANGE_ME_TO_YOUR_SECRET_TOKEN' 改成你自己的密碼再執行！
-- 網頁端輸入這個密碼才能看到個人數據。
insert into app_settings (key, value)
values ('dashboard_token_hash',
        encode(digest('CHANGE_ME_TO_YOUR_SECRET_TOKEN', 'sha256'), 'hex'))
on conflict (key) do update set value = excluded.value;

-- ── 網頁用 RPC：驗證 token 後回傳 Dashboard 所需全部數據 ──────
create or replace function dashboard_data(p_token text)
returns jsonb
language plpgsql
security definer
set search_path = public
as $$
declare
  stored_hash text;
begin
  select value into stored_hash from app_settings where key = 'dashboard_token_hash';
  if stored_hash is null
     or encode(digest(p_token, 'sha256'), 'hex') <> stored_hash then
    return null;  -- token 錯誤：不回任何資料
  end if;

  return jsonb_build_object(
    'status', (select to_jsonb(b) from bot_status b where id = 1),
    'earnings', (
      select coalesce(jsonb_agg(to_jsonb(e) order by e.date), '[]'::jsonb)
      from earnings e
      where e.date > current_date - interval '30 days'
    ),
    'snapshots', (
      select coalesce(jsonb_agg(jsonb_build_object(
               'ts', s.ts, 'anchor_apy', s.anchor_apy,
               'frr', s.frr, 'spike', s.spike) order by s.ts), '[]'::jsonb)
      from (
        select * from market_snapshots
        where ts > now() - interval '24 hours'
        order by ts desc limit 288
      ) s
    ),
    'recent_actions', (
      select coalesce(jsonb_agg(to_jsonb(a) order by a.ts desc), '[]'::jsonb)
      from (
        select ts, action, detail from actions_log
        order by ts desc limit 20
      ) a
    )
  );
end;
$$;

-- 只開放這個 RPC 給 anon（網頁用 anon key 呼叫）
revoke all on function dashboard_data(text) from public;
grant execute on function dashboard_data(text) to anon;

-- ── （選用）自動清理舊資料：機器人每天也會清，這是雙保險 ──
-- 若有開 pg_cron extension 可取消下面註解：
-- select cron.schedule('prune-snapshots', '0 4 * * *',
--   $$delete from market_snapshots where ts < now() - interval '30 days';
--     delete from credits_snapshots where ts < now() - interval '30 days';
--     delete from actions_log where ts < now() - interval '90 days';$$);
