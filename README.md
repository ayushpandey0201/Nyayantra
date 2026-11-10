# Nyayantra

A research assistant for Indian legal research and contract analysis, built with retrieval-augmented generation on Groq, with compliance controls at every stage of the pipeline. Every pipeline stage is its own API route, and every response carries a trace with each stage's output, latency, the exact prompts sent, the raw model replies and the controls applied.

It is a research aid for advocates, not legal advice (Advocates Act 1961, ss. 29 and 33).

## Run

Everything goes through `./app.sh`:

```bash
./app.sh setup     # first time: virtualenv, packages, .env with service keys, database
./app.sh           # start the API on port 8010 and open the console
./app.sh keys user # copy a key to the clipboard for the console (keys are never printed)
./app.sh demo      # walk through every endpoint and the control it applies
./app.sh test      # 162 offline tests with coverage   (./app.sh test --live for Groq and Gemini)
./app.sh docs      # rebuild the slides and the report
./app.sh stop
```

Add `GROQ_API_KEY` (and `GEMINI_API_KEY` for the public fallback) to `.env` after setup. Under the hood the server is `.venv/bin/uvicorn nyayantra.api:app --port 8010`.

Open `http://127.0.0.1:8010/console` for the test console, or `http://127.0.0.1:8010/docs` for the OpenAPI page. In the console, paste `NYAYANTRA_API_KEY` (users) and `NYAYANTRA_REVIEWER_KEY` (advocates) from `.env`. Only the reviewer key can decide reviews, read the audit log and erase records.

Requires PostgreSQL 17 with pgvector (`host=/tmp dbname=nyayantra`). Set up the schema with `db/schema.sql`, then `db/migrations/*.sql`. The local models (BGE-M3 and bge-reranker-v2-m3) load in the background when the server starts.

## Routes

| Stage | Route | What it does |
|---|---|---|
| 1 | `POST /v1/redact` | Removes personal data and party names; keeps legal citations |
| 2 | `POST /v1/screen` | Injection screen: rules plus Llama Prompt Guard 2 |
| 3 | `POST /v1/understand` | Language, as-of date and domains; checks every cited authority against the database |
| 4 | `POST /v1/retrieve` | Hybrid search (dense, sparse, full-text, named case), reranking, legal status on the date |
| 5 | `POST /v1/generate` | Answers only from passages fetched by id (gpt-oss-120b) |
| 6 | `POST /v1/guardrail` | Fail-closed checks on a draft: withhold, review or pass |
| flow | `POST /v1/research` | Stages 1–6, with one repair round |
| flow | `POST /v1/contracts/analyze` | Clause review; confidential, so Groq only; always goes to an advocate |
| flow | `POST /v1/citations/verify` | Checks every citation in a draft against the database (no model) |
| review | `GET /v1/reviews`, `POST /v1/reviews/{id}/decision` | The advocate approves, corrects or rejects; one decision per request |
| audit | `GET /v1/audit`, `GET /v1/audit/verify` | SHA-256 hash chain of requests, reviews and erasures |
| erasure | `POST /v1/erase`, `POST /v1/retention/purge` | DPDP Act s.12: removes stored text, trace and LLM cache rows |
| ops | `/health`, `/v1/compliance/map`, `/v1/sources`, `/v1/units/{id}`, `/v1/requests` | Status, control map, corpus |

Stored results are redacted. The audit log keeps ids and hashes only, and the database refuses updates or deletes on it.

## Tests and evaluation

```bash
.venv/bin/python -m pytest -q tests --cov=nyayantra # offline: 156 tests; a scripted provider stands in for Groq/Gemini
NYAYANTRA_LIVE=1 .venv/bin/python -m pytest -q tests/test_live.py   # 9 live tests against Groq and Gemini
.venv/bin/python -m eval.retrieval_eval             # recall@5 on 78 gold questions
.venv/bin/python -m eval.answers_smoke              # live: 8 questions, 10 invented-citation traps, 2 foreign-law
.venv/bin/python -m eval.clauses_eval               # live: 24 labelled synthetic clauses plus an injection
```

See `docs/DEMO.md` for a walkthrough of every endpoint and the control it applies, `docs/EVALUATION.md` for the results, `docs/PLAN.md` for the design and `docs/PROJECT_BRIEF.md` for the brief.

The offline tests need the local database but no network: `tests/conftest.py` replaces only the HTTP call, so the real routing, fallback, cache and trace code runs, and every request body that would have been sent can be inspected. The store tests use a separate `nyayantra_test` database, so the real audit chain is never touched.

## Slides and report

`docs/Nyayantra_AAT_Presentation.pptx` (and `.pdf`) and `docs/Nyayantra_AAT_Report.docx` (and `.pdf`) are generated:

```bash
.venv/bin/python docs/deck/build_deck.py       # 29 slides on the BMSCE deck template
.venv/bin/python docs/report/build_report.py   # report on the BMSCE report template
```

Both read their templates from `docs/reference/` (local only). Console screenshots in `docs/figures/console` were captured from the running console. For the report's contents page numbers, export the .docx to PDF (Pages or Word), run `docs/report/paginate.py <pdf>`, then build again.
