"""Machine-readable control map: what each stage does and which rule it implements. Every route's
trace carries the controls it applied; this map is the index the console and the report use."""

CONTROLS = {
    "1_redact": {
        "route": "POST /v1/redact",
        "control": "Personal data (Aadhaar with check digit, PAN, phone, email, bank, card, passport, voter id, GSTIN) "
                   "and contract party names replaced by placeholders before any model call; legal citations kept",
        "refs": ["DPDP Act 2023 s.8(5) reasonable security safeguards; s.8(7) erase when purpose served",
                 "Bharatiya Sakshya Adhiniyam 2023 s.132 professional communications (privilege)",
                 "BCI Rules Part VI Ch. II r.17 (confidence of client)", "OWASP LLM02 Sensitive Information Disclosure"]},
    "2_screen": {
        "route": "POST /v1/screen",
        "control": "Rules (English and Hindi) plus Llama Prompt Guard 2 over overlapping windows; flagged questions are not sent "
                   "to the model; flagged contract text is reviewed as data and the report goes to an advocate",
        "refs": ["OWASP LLM01 Prompt Injection", "IT Act 2000 s.43 / s.66 (unauthorised manipulation of a computer system)"]},
    "3_understand": {
        "route": "POST /v1/understand",
        "control": "Language, as-of date, domain and every cited authority checked against the database; "
                   "invented sections and cases are marked unverified before generation",
        "refs": ["Advocates Act 1961 s.35 and BCI Rules: duty of competence and care", "OWASP LLM09 Misinformation"]},
    "4_retrieve": {
        "route": "POST /v1/retrieve",
        "control": "Hybrid search (dense, sparse, full-text, named-case) with reranking; law in force on the as-of date; "
                   "repealed provisions replaced by mapped successors (IPC -> BNS, Evidence Act -> BSA); overruled judgments dropped",
        "refs": ["BNS 2023 s.358 and BSA 2023 s.170 (repeal and savings)", "General Clauses Act 1897 s.6 (effect of repeal)",
                 "OWASP LLM08 Vector and Embedding Weaknesses (curated, dated, licensed corpus)"]},
    "5_generate": {
        "route": "POST /v1/generate",
        "control": "Answer only from the supplied passages, cited by unit id; explains the law, does not advise a client "
                   "or predict an outcome; confidential requests use Groq only",
        "refs": ["Advocates Act 1961 ss.29, 33 (only advocates may practise law)", "BCI Rules Part VI Ch. II r.36 (no solicitation)",
                 "OWASP LLM10 Unbounded Consumption (token caps, throttling)"]},
    "6_guardrail": {
        "route": "POST /v1/guardrail",
        "control": "Fail-closed checks: schema, every cited id was in context, no invented law, quotes verbatim, unverified "
                   "authorities refused, no personal data or prompt in the output, repealed status disclosed; "
                   "policy model (gpt-oss-safeguard-20b) for legal-advice overreach",
        "refs": ["OWASP LLM05 Improper Output Handling; LLM07 System Prompt Leakage; LLM09 Misinformation",
                 "Advocates Act 1961 ss.29, 33"]},
    "7_review": {
        "route": "POST /v1/reviews/{id}/decision",
        "control": "Withheld, flagged and every contract result is queued for an advocate, who approves, corrects or rejects it; "
                   "one decision per request, recorded in the audit log",
        "refs": ["Advocates Act 1961 s.35 (advocate accountable for the advice given)", "ISO/IEC 42001 Annex A.9 (human oversight)",
                 "NIST AI RMF GOVERN 3.2, MANAGE 2.4"]},
    "8_audit": {
        "route": "GET /v1/audit/verify",
        "control": "SHA-256 hash chain of every request, review and erasure (hashes and ids only, no client text); "
                   "the database refuses updates and deletes on the log",
        "refs": ["DPDP Act 2023 s.8(1) accountability of the data fiduciary", "ISO/IEC 42001 Annex A.6.2.8 (event logs)",
                 "IT Act 2000 s.67C (preservation of records by intermediaries, as a benchmark)"]},
    "9_erase": {
        "route": "POST /v1/erase",
        "control": "Erase a request's stored text, trace and LLM cache rows on request or when the retention period ends; "
                   "the erasure itself is audited",
        "refs": ["DPDP Act 2023 s.12 (right to erasure); s.8(7) (erase once the purpose is served)",
                 "DPDP Rules 2025 r.8 (retention periods)"]},
    "routing": {
        "route": "all LLM calls",
        "control": "Confidential requests (personal data found, or any contract) go to Groq only; public questions may fall "
                   "back to Gemini; every response cached by request hash so results are reproducible",
        "refs": ["DPDP Act 2023 s.8(5)", "BSA 2023 s.132", "Gemini API free-tier terms: inputs may be used to improve products"]},
}
