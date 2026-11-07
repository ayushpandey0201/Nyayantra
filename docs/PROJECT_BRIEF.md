# Project brief

**Course:** Hybrid Intelligence and Large Language Models (26AI7PELLM), BMSCE. AAT topic 5, Legal Services: LLMs for legal research and contract analysis, examining confidentiality, intellectual property, hallucination risks, accountability and legal ethics.

**Goal:** a real, demonstrable application on Indian law, not only a report. The professor already has the compliance theory; she wants to see it implemented.

## Approach
- RAG over an Indian legal corpus (bare Acts from India Code, selected Supreme Court judgments). The legal corpus is lawful to reproduce under Copyright Act s.52(1)(q).
- Metadata on every passage: Act, section, in force or repealed, effective date, replaced by (for example IPC to BNS from 1 July 2024).
- Hybrid retrieval with metadata filters; a small citation map for repealed-to-new provisions.
- LLM: Groq (free tier), key in `.env` as `GROQ_API_KEY`.
- Deterministic code checks after the LLM: every cited section must exist in the corpus; quotes must match; injected instructions in contracts are caught.
- Human review by an advocate, and a tamper-evident audit log.

## Features
1. Legal research Q&A with verifiable citations.
2. Contract clause review against Indian law (Contract Act s.27, s.28, s.74; Arbitration Act; DPDP terms).
3. Citation checker (repealed provisions, invented cases).

## Compliance at each stage
| Stage | Indian law | Control |
|---|---|---|
| Upload | DPDP Act s.8(5); privilege under BSA s.132 | Redact client names, PAN, Aadhaar, phone before Groq |
| Prompt | Advocates Act s.29, s.33 | Research aid, not advice; answer only from retrieved law; contract text treated as data |
| Retrieval | Duty of competence | In-force filter; old-to-new mapping |
| Output | Hallucination; OWASP LLM01, LLM09 | Citation and quote verification; injection checks |
| Review | Advocates Act s.35 | Advocate approves, edits or rejects |
| Audit | Accountability; DPDP erasure | Hash-chained log; erase endpoint |

## Constraints
- Groq is hosted outside India: demo uses synthetic contracts only.
- Every API route is separately callable and shown live in the OpenAPI docs; each trace includes the exact prompt and raw reply.
- Results must come from a real test run (including invented-case trap questions).
