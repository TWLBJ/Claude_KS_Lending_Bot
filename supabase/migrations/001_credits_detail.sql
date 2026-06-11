-- 2026-06-11：放貸明細 + 已結束單
-- 1) bot_status 加 credits 欄位（每筆放貸詳情）
-- 2) dashboard_data RPC 回傳 closed_credits（近 10 筆已結束單）

alter table bot_status add column if not exists credits jsonb;

create or replace function dashboard_data(p_token text)
returns jsonb
language plpgsql
security definer
set search_path = public, extensions
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
    'statuses', (
      select coalesce(jsonb_agg(to_jsonb(b) order by b.symbol), '[]'::jsonb)
      from bot_status b
    ),
    'earnings', (
      select coalesce(jsonb_agg(to_jsonb(e) order by e.date), '[]'::jsonb)
      from earnings e
      where e.date > current_date - interval '30 days'
    ),
    'snapshots', (
      select coalesce(jsonb_agg(jsonb_build_object(
               'ts', s.ts, 'symbol', s.symbol, 'anchor_apy', s.anchor_apy,
               'frr', s.frr, 'spike', s.spike) order by s.ts), '[]'::jsonb)
      from (
        select * from market_snapshots
        where ts > now() - interval '24 hours'
        order by ts desc limit 576
      ) s
    ),
    'recent_actions', (
      select coalesce(jsonb_agg(to_jsonb(a) order by a.ts desc), '[]'::jsonb)
      from (
        select ts, action, detail from actions_log
        order by ts desc limit 20
      ) a
    ),
    'closed_credits', (
      select coalesce(jsonb_agg(to_jsonb(a) order by a.ts desc), '[]'::jsonb)
      from (
        select ts, action, detail from actions_log
        where action in ('closed_matured', 'closed_early')
        order by ts desc limit 10
      ) a
    )
  );
end;
$$;

revoke all on function dashboard_data(text) from public;
grant execute on function dashboard_data(text) to anon;
