# Nyayantra: build plan

## Context
Ayush's AAT (26AI7PELLM, topic 5: LLMs for Legal Research and Contract Analysis) must be a working application on Indian law, not only a report. The professor already has the compliance theory and wants to see it implemented. Nyayantra is a RAG system on Groq where every pipeline stage is a separate API route, every response carries the exact prompt, raw model reply and the compliance controls applied, and results come from a real test run. Repo: `~/Developer/ayush_projects/Nyayantra` (git initialised; `docs/reference/` and `.env` are git-ignored; Groq key verified working).

**Decided with Ayush:** Postgres + pgvector (single database); ~15 Acts + 50 judgments; benchmark spread over 2–3 days on the free Groq tier with a response cache. Team on the cover: Ayush Pandey (1BM23AD074), Sharanya Sandur (1BM23AD055), to be confirmed.

## Stack decisions (with reasons)

| Concern | Choice | Why |
|---|---|---|
| Database | **PostgreSQL 17** (already installed via Homebrew) | Acts → sections → amendments → judgments is relational data. Citation checks become SQL lookups. Runs natively, no Docker (8 GB RAM) |
| Vector store | **pgvector** extension (`brew install pgvector`), `halfvec` HNSW index | Free, open source, lives inside the same DB, so metadata filters are plain `WHERE` clauses. At ~10k chunks, Qdrant's filtered-HNSW advantage does not matter |
| Lexical search | Postgres full-text (`tsvector`, GIN) + `pg_trgm` for citation aliases | Legal queries are full of exact terms ("section 27", "restraint of trade"); dense vectors alone miss these |
| Embeddings | **BAAI/bge-m3** (local, MPS/CPU, fp16) | Multilingual (Hindi queries), 8,192-token input (long sections), outperforms multilingual-e5-large on MIRACL in its paper. Groq has no embedding API |
| Reranker | **BAAI/bge-reranker-v2-m3** (local) | Reorders top 20 into top 5; biggest quality gain for small corpora |
| Main LLM | Groq `openai/gpt-oss-120b`, `reasoning_effort: low` | Best model available on this account |
| Helper LLM | Groq `openai/gpt-oss-20b` | Query rewriting and claim extraction, cheaper |
| Injection guard | Groq `meta-llama/llama-prompt-guard-2-86m` | A real classifier for OWASP LLM01 on uploaded contracts, alongside regex |
| Policy guard | Groq `openai/gpt-oss-safeguard-20b` | Checks answers for "legal advice" overreach (Advocates Act s.29, s.33) |
| API | FastAPI + uvicorn, OpenAPI docs | Same pattern as the Topic 8 project |
| UI | Single-page test console (HTML/JS, no build step) | Shows every stage live; doubles as slide screenshots |

**Groq budget (verify at console.groq.com):** free tier for gpt-oss-120b is about 30 req/min, 1,000 req/day, 8,000 tokens/min, **200,000 tokens/day**. A full benchmark needs roughly 0.6–1M tokens, so: cache every LLM response by request hash, keep context to 5 passages, cap output tokens, and run the benchmark across 2–3 days or use gpt-oss-20b for bulk configs.

## Where the documents come from

| Source | What we take | Licence / legal basis |
|---|---|---|
| India Code (indiacode.nic.in), Ministry of Law | Bare Act PDFs, current consolidated versions | Reproducing Acts is permitted under Copyright Act 1957 s.52(1)(q); check the site's terms page before fetching. Polite fetch: one file at a time, cached |
| MeitY / egazette.gov.in | DPDP Act 2023, DPDP Rules 2025, IT Rules 2021 as amended | Official gazette text |
| Bar Council of India | BCI Rules Part VI, Ch. II (professional conduct and etiquette) | Official rules |
| AWS Open Data: `indian-supreme-court-judgments` (Dattam Labs, mirrored from eCourts) | 40–60 hand-picked Supreme Court judgments + its parquet metadata | **CC-BY-4.0**; no AWS account needed (`--no-sign-request`) |
| IPC → BNS correspondence | ~120 most-used mappings | No official table found yet: build from a reputable table and **verify every row against both bare Acts**; store `verified=true` only after the check |

**Starter corpus (~15 instruments):** Indian Contract Act 1872; Specific Relief Act 1963; Arbitration and Conciliation Act 1996; Limitation Act 1963; Sale of Goods Act 1930; Information Technology Act 2000; DPDP Act 2023 + DPDP Rules 2025; Copyright Act 1957; Advocates Act 1961 + BCI Rules Part VI; Bharatiya Nyaya Sanhita 2023; Bharatiya Sakshya Adhiniyam 2023; Indian Penal Code 1860 (marked repealed, kept for mapping); Indian Evidence Act 1872 (repealed, kept for mapping); Consumer Protection Act 2019.

**Judgments (examples):** Puttaswamy (privacy), Percept D'Mark (s.27 non-compete), Kailash Nath (s.74 penalty), BALCO and Vidya Drolia (arbitration), Shreya Singhal (IT Act s.66A), plus others chosen for the test questions.

## Metadata design

Three levels, with stable IDs, provenance on everything, and time validity so the system can answer "what was the law on date X".

**1. `instrument` (one per Act, Rules or judgment)**
`instrument_id` (slug, e.g. `ICA-1872`), `kind` (act / rules / regulation / judgment), `short_title`, `long_title`, `act_number` ("Act No. 9 of 1872"), `jurisdiction` (`IN` central or state code), `enacted_on`, `commenced_on`, `status` (in_force / repealed / partly_in_force), `repealed_by`, `repealed_on`, `as_amended_upto`, `domains[]` (contract, criminal, evidence, privacy, arbitration, IP, ethics), `language`, `source_url`, `retrieved_at`, `source_sha256`, `licence_basis`.
Judgment extras: `neutral_citation`, `reporter_citations[]` (e.g. "(2017) 10 SCC 1"), `court`, `bench_size`, `judges[]`, `decided_on`, `parties`, `statutes_cited[]` (normalised unit IDs), `treatment` (good law / overruled, curated), `dataset` and `dataset_licence`.

**2. `unit` (the retrieval unit: section, sub-section, proviso, explanation, illustration, schedule entry, rule, judgment paragraph)**
`unit_id` (e.g. `ICA-1872:s27`, `BNS-2023:s318(4)`, `SC-2017-PUTTASWAMY:p184`), `instrument_id`, `parent_id`, `unit_type`, `number`, `heading`, `path` ("Part II > Chapter II > s.27"), `text`, `canonical_citation` ("Indian Contract Act, 1872, s. 27"), `aliases[]` ("ICA s27", "Contract Act 27"), `valid_from`, `valid_to`, `amended_by[]` (amending Act and date), `cross_refs[]` (outgoing unit IDs, regex-extracted then checked), `defined_terms[]`, `topics[]` (non-compete, penalty, arbitration seat, consent…), `clause_types[]` (which contract clause types it governs), `source_page`, `char_start`/`char_end`, `token_count`, `content_sha256`.

**3. `chunk` (what gets embedded)**
`chunk_id`, `unit_id`, `context_header` ("Indian Contract Act 1872 > Chapter II > s.27 Agreement in restraint of trade, void"), `embed_text` (header + text), `dense` (halfvec 1024), `sparse_terms` (BGE-M3 lexical weights, JSON), `tsv` (full-text), `embedding_model` + `model_version`, `ingested_at`.

**Plus:** `mapping` (old unit → new unit, `relation` = same / modified / merged / split / omitted, `verified`, `source`), `citation_index` (every canonical form and alias → unit_id, used by the guardrail), and app tables `request`, `review`, `audit_log`.

**Chunking:** structure-aware. Each section is one unit; long sections split at sub-sections; illustrations and provisos stay attached to their section; the context header is prepended before embedding. Judgments split by numbered paragraph.

**Metadata quality checks (run on every ingest):** section numbering has no gaps (or each gap is explained, e.g. an omitted section), every `cross_ref` resolves, every repealed instrument has a `repealed_by`, every mapping row is verified, hashes stable on re-ingest, 30-section manual spot check against the PDF.

## Pipeline and API routes

| # | Stage | Route | Compliance served |
|---|---|---|---|
| 1 | Redact client data | `POST /v1/redact` | DPDP s.8(5); privilege under BSA s.132 |
| 2 | Injection check (regex + Prompt Guard) | `POST /v1/screen` | OWASP LLM01 |
| 3 | Understand query (cites, dates, domain) | `POST /v1/understand` | Accuracy (as-of date) |
| 4 | Hybrid retrieve + rerank, metadata filters | `POST /v1/retrieve` | Duty of competence; in-force filter; IPC→BNS mapping |
| 5 | Generate answer, cite unit IDs only | `POST /v1/generate` | Advocates Act s.29, s.33 (research aid, not advice) |
| 6 | Guardrail: citations exist, in force, quotes match; policy guard | `POST /v1/guardrail` | Hallucination; OWASP LLM05, LLM09 |
| 7 | Advocate review | `POST /v1/reviews/{id}/decision` | Advocates Act s.35 (accountability) |
| 8 | Audit and erasure | `GET /v1/audit/verify`, `POST /v1/erase` | DPDP s.12; accountability |
| — | Whole flow | `POST /v1/research`, `POST /v1/contracts/analyze`, `POST /v1/citations/verify` | All of the above |
| — | Ops | `GET /health`, `/v1/compliance/map`, `/v1/sources`, `/v1/units/{id}` | Transparency |

Every response returns a trace: each stage's input, output, latency, the exact system and user prompt, raw Groq reply, token counts, model, prompt version, and the compliance clauses applied.

## Repo layout
`nyayantra/` (app: `api.py`, `config.py`, `llm.py`, `redact.py`, `screen.py`, `retrieve.py`, `generate.py`, `guardrail.py`, `audit.py`, `compliance.py`, `prompts.py`), `ingest/` (`sources.yaml`, `fetch.py`, `parse_act.py`, `parse_judgment.py`, `metadata.py`, `embed.py`, `validate.py`), `db/schema.sql`, `data/` (raw and processed, git-ignored), `eval/`, `tests/`, `console/`, `docs/`.

## Reuse from `docs/reference/`
- `topic8_misinformation_worked_example/verifeed/llm.py`: OpenAI-compatible client, works with Groq as-is.
- `.../verifeed/stages.py`: redaction regexes, `AuditLog` hash chain, guardrail check pattern.
- `.../verifeed/compliance.py` and `app.py`: per-stage control map, route and auth pattern.
- `.../verifeed/console.html`, `deck/capture.py`: test console and screenshot capture.
- `.../eval/run_eval.py`, `analyze.py`: resumable benchmark runner and scoring.
- `.../deck/lib.py`, `build2.py`, `report/build_report.py`, `report/paginate.py`: slides and report later.
- `legal_services_v1/poc/data/questions.json` (31 questions with gold sections, 10 invented-case traps), `clauses.json` (24 labelled clauses), `pipeline.py` citation regex.

## Phases
1. **Database and schema:** install pgvector, create `nyayantra` DB, write `db/schema.sql`.
2. **Ingestion:** fetch sources, parse Acts into units, build metadata, IPC→BNS mapping with verification, judgments from AWS, run quality checks.
3. **Embeddings:** BGE-M3 dense + sparse for every chunk; HNSW and GIN indexes.
4. **Retrieval:** hybrid search with reciprocal rank fusion, metadata filters, as-of date, reranker; measure recall@5 on gold questions before any LLM work.
5. **LLM stages:** prompts, Groq calls with response cache, guardrail, policy guard.
6. **API, console, review and audit.**
7. **Tests:** unit tests per stage, contract tests for Groq format, live end-to-end tests.
8. **Benchmark:** C1 LLM only; C2 dense RAG; C3 hybrid + rerank + metadata; C4 full Nyayantra. Test set: ~50 research questions (gold units), 10 invented-case traps, 15 repealed-citation questions (IPC/Evidence Act), 24 contract clauses, injection and PII cases. Metrics: retrieval recall@5, correct answers, unsupported citations, trap refusals, repealed-law handling, clause precision/recall, latency, tokens.
9. **Slides and report** in the BMSCE templates (separate step after results).

## Verification
- `ingest/validate.py` passes all metadata checks; spot check 30 sections against source PDFs.
- Retrieval recall@5 ≥ 85% on gold questions before LLM stages are added.
- `pytest` green, including live tests that call Groq and check the redacted prompt.
- Every route callable from `/docs`; console run of each test case.
- Benchmark finishes with zero HTTP errors; results reproducible from the response cache.
