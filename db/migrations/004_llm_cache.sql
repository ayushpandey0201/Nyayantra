-- Every LLM response, keyed by a hash of (provider, model, messages, parameters). Re-running a request
-- or the benchmark replays the stored reply instead of calling the provider again. Prompts here are
-- already redacted (Stage 1 runs before any LLM call). Not dropped by schema.sql, like embed_cache.
create table if not exists llm_cache (
    cache_key   text primary key,
    provider    text not null,
    model       text not null,
    request     jsonb not null,
    response    jsonb not null,
    created_at  timestamptz not null default now()
);
