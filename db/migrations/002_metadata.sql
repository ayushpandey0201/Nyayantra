-- 002: finer legal metadata (applied on top of schema.sql v1; schema.sql already includes it).
alter table instrument
    add column if not exists bench_line          text,                         -- bench as printed in the S.C.R.
    add column if not exists statutes_cited      text[] not null default '{}', -- case level (curated)
    add column if not exists cites_judgments     text[] not null default '{}', -- other corpus judgments it cites
    add column if not exists cited_by_judgments  text[] not null default '{}',
    add column if not exists overruled_by        text references instrument(instrument_id) deferrable initially deferred,
    add column if not exists overruled_on        date,
    add column if not exists commencement_source text,                         -- notification the dates come from
    add column if not exists metadata_notes      jsonb not null default '{}';  -- field -> how it was derived

alter table unit
    add column if not exists text_valid_from   date,      -- current wording in force since (latest amendment)
    add column if not exists commencement_note text,
    add column if not exists opinion_author    text,      -- judgment paragraphs: the judge whose opinion it is
    add column if not exists state_amendments  jsonb not null default '[]',  -- [{"state", "text"}], kept out of text
    add column if not exists external_refs     text[] not null default '{}', -- references to law outside the corpus
    add column if not exists cites_judgments   text[] not null default '{}', -- judgment instrument_ids cited here
    drop column if exists char_start,
    drop column if exists char_end;

create table if not exists defined_term (
    term          text not null,          -- as printed: 'data fiduciary'
    term_norm     text not null,          -- lower-case, single spaces
    instrument_id text not null references instrument(instrument_id) on delete cascade,
    unit_id       text not null references unit(unit_id) on delete cascade,
    clause        text not null default '',  -- '(i)' within s.2; '' if none
    verb          text not null,          -- means / includes / denotes / is said to ...
    definition    text not null,
    primary key (term_norm, unit_id, clause)
);
create index if not exists defined_term_trgm on defined_term using gin(term_norm gin_trgm_ops);
create index if not exists unit_author_idx on unit(opinion_author) where opinion_author is not null;
