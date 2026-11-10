-- Phase 6: request decisions, review queue details, erasure, and an append-only audit log.
alter table request add column if not exists decision     text;
alter table request add column if not exists sensitivity  text;
alter table request add column if not exists cache_keys   text[];      -- LLM cache rows made for this request (erased with it)
alter table request add column if not exists erased_at    timestamptz;
create index if not exists request_created on request(created_at desc);

alter table review add column if not exists queued_at  timestamptz not null default now();
alter table review add column if not exists reason     text;           -- why the request needs an advocate
alter table review add column if not exists edited     jsonb;          -- the advocate's corrected answer, if any
create index if not exists review_status on review(status, queued_at);

-- The audit log can only grow: updates and deletes are refused by the database itself.
create or replace function audit_log_append_only() returns trigger language plpgsql as $$
begin
    raise exception 'audit_log is append-only';
end $$;
drop trigger if exists audit_log_no_change on audit_log;
create trigger audit_log_no_change before update or delete on audit_log
    for each row execute function audit_log_append_only();
