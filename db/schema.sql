-- Nyayantra schema. Three levels of legal metadata (instrument > unit > chunk),
-- a verified map from repealed to replacement provisions, a citation index used by the
-- guardrail, and the application tables (requests, reviews, audit log).
-- Safe to re-run: drops and recreates everything.

create extension if not exists vector;
create extension if not exists pg_trgm;

drop table if exists audit_log, review, request, citation_index, mapping, chunk, unit, instrument cascade;
drop type if exists instrument_kind, instrument_status, unit_kind, map_relation cascade;

create type instrument_kind   as enum ('act', 'rules', 'regulation', 'judgment');
create type instrument_status as enum ('in_force', 'repealed', 'partly_in_force');
create type unit_kind as enum ('part', 'chapter', 'section', 'sub_section', 'clause', 'proviso',
                               'explanation', 'illustration', 'schedule', 'rule', 'paragraph');
create type map_relation as enum ('same', 'modified', 'merged', 'split', 'omitted', 'new');

-- 1. One row per Act, set of Rules or judgment.
create table instrument (
    instrument_id     text primary key,               -- e.g. ICA-1872, BNS-2023, SC-2017-PUTTASWAMY
    kind              instrument_kind not null,
    short_title       text not null,
    long_title        text,
    act_number        text,                           -- "Act No. 9 of 1872"
    jurisdiction      text not null default 'IN',     -- IN = central; state codes like IN-KA
    enacted_on        date,
    commenced_on      date,
    status            instrument_status not null default 'in_force',
    repealed_by       text references instrument(instrument_id) deferrable initially deferred,
    repealed_on       date,
    as_amended_upto   date,
    domains           text[] not null default '{}',   -- contract, criminal, evidence, privacy, arbitration, ip, ethics ...
    language          text not null default 'en',
    source_url        text,
    retrieved_at      timestamptz,
    source_sha256     text,
    licence_basis     text not null,                  -- e.g. 'Copyright Act 1957 s.52(1)(q)', 'CC-BY-4.0'
    -- judgment-only fields
    neutral_citation  text,
    reporter_citations text[] not null default '{}',  -- "(2017) 10 SCC 1"
    court             text,
    bench_size        int,
    judges            text[] not null default '{}',
    decided_on        date,
    parties           text,
    treatment         text,                           -- good_law / overruled / partly_overruled (curated)
    dataset           text,
    parent_id         text references instrument(instrument_id),   -- Rules -> parent Act
    ocr               boolean not null default false,              -- text came from a scan
    created_at        timestamptz not null default now(),
    check (status <> 'repealed' or (repealed_by is not null and repealed_on is not null)),
    check (kind <> 'judgment' or decided_on is not null)
);

-- 2. The retrieval unit: a section, sub-section, proviso, illustration, rule or judgment paragraph.
create table unit (
    unit_id            text primary key,              -- ICA-1872:s27, BNS-2023:s318(4), SC-2017-PUTTASWAMY:p184
    instrument_id      text not null references instrument(instrument_id) on delete cascade,
    parent_id          text references unit(unit_id) on delete cascade,
    unit_type          unit_kind not null,
    number             text,                          -- '27', '318(4)', '184'
    heading            text,
    path               text,                          -- 'Chapter II > s.27'
    text               text not null,
    canonical_citation text not null,                 -- 'Indian Contract Act, 1872, s. 27'
    aliases            text[] not null default '{}',
    valid_from         date,
    valid_to           date,                          -- null = still valid
    amended_by         jsonb not null default '[]',   -- [{"by": "Act 18 of 1996", "on": "1996-..."}]
    cross_refs         text[] not null default '{}',  -- outgoing unit_ids
    defined_terms      text[] not null default '{}',
    topics             text[] not null default '{}',  -- non_compete, penalty, arbitration_seat, consent ...
    clause_types       text[] not null default '{}',  -- contract clause types this unit governs
    statutes_cited     text[] not null default '{}',  -- for judgment paragraphs
    sort_key           int not null default 0,
    source_page        int,
    char_start         int,
    char_end           int,
    token_count        int,
    content_sha256     text not null,
    omitted            boolean not null default false,  -- repealed/omitted provision kept for history
    ocr                boolean not null default false,
    check (valid_to is null or valid_from is null or valid_to >= valid_from)
);
create index unit_instrument_idx on unit(instrument_id, sort_key);
create index unit_topics_idx     on unit using gin(topics);
create index unit_clause_idx     on unit using gin(clause_types);
create index unit_validity_idx   on unit(valid_from, valid_to);

-- 3. What gets embedded and searched.
create table chunk (
    chunk_id         text primary key,                -- unit_id + '#' + n
    unit_id          text not null references unit(unit_id) on delete cascade,
    seq              int not null default 0,
    context_header   text not null,                   -- 'Indian Contract Act, 1872 > Chapter II > s.27 Agreement in restraint of trade, void'
    embed_text       text not null,                   -- header + text
    dense            halfvec(1024),                   -- BAAI/bge-m3 dense
    sparse_terms     jsonb,                           -- BAAI/bge-m3 lexical weights {token: weight}
    tsv              tsvector generated always as (to_tsvector('english', embed_text)) stored,
    embedding_model  text,
    model_version    text,
    ingested_at      timestamptz not null default now()
);
create index chunk_unit_idx  on chunk(unit_id);
create index chunk_tsv_idx   on chunk using gin(tsv);
create index chunk_dense_idx on chunk using hnsw (dense halfvec_cosine_ops) with (m = 16, ef_construction = 64);

-- Repealed or renumbered provision -> its replacement (e.g. IPC s.420 -> BNS s.318(4)).
create table mapping (
    mapping_id   bigserial primary key,
    old_unit_id  text not null references unit(unit_id) on delete cascade,
    new_unit_id  text references unit(unit_id) on delete cascade,   -- null when omitted
    relation     map_relation not null,
    note         text,
    source       text not null,                     -- where the correspondence came from
    verified     boolean not null default false,    -- true only after checking both bare Acts
    verified_on  date
);
create unique index mapping_uq on mapping(old_unit_id, coalesce(new_unit_id, ''));

-- Every citation form the guardrail accepts -> the unit it means.
create table citation_index (
    form      text not null,                         -- normalised: 'ica 1872 s 27', 'contract act s 27', '(2017) 10 scc 1'
    unit_id   text references unit(unit_id) on delete cascade,
    instrument_id text references instrument(instrument_id) on delete cascade,
    primary key (form, unit_id)
);
create index citation_form_trgm on citation_index using gin(form gin_trgm_ops);

-- Application tables.
create table request (
    request_id      text primary key,
    kind            text not null,                   -- research / contract / citation_check
    created_at      timestamptz not null default now(),
    redacted_input  text,                            -- never the raw input
    input_sha256    text not null,
    as_of           date,
    result          jsonb,                           -- full trace
    retain_until    date
);

create table review (
    request_id   text primary key references request(request_id) on delete cascade,
    status       text not null default 'pending',   -- pending / approved / edited / rejected
    reviewer     text,
    note         text,
    decided_at   timestamptz
);

create table audit_log (
    seq        bigserial primary key,
    ts         timestamptz not null default now(),
    event      text not null,
    body       jsonb not null,                       -- hashes and ids only, no raw client text
    prev_hash  text not null,
    hash       text not null unique
);
