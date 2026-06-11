-- 2026-06-11：觀察模式的建議掛單（網頁顯示用）
alter table bot_status add column if not exists suggested_offers jsonb;
