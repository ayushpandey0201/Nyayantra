-- Embeddings keyed by the hash of the exact text embedded, independent of unit/chunk rows,
-- so reloading units (which cascades to chunk) does not throw away an hour of embedding work.
create table if not exists embed_cache (
    sha256          text not null,
    embedding_model text not null,
    model_version   text,
    dense           halfvec(1024) not null,
    sparse_terms    jsonb not null,
    created_at      timestamptz not null default now(),
    primary key (sha256, embedding_model)
);
insert into embed_cache (sha256, embedding_model, model_version, dense, sparse_terms)
select encode(sha256(convert_to(embed_text, 'UTF8')), 'hex'), embedding_model, model_version, dense, sparse_terms
from chunk where dense is not null and sparse_terms is not null
on conflict do nothing;
