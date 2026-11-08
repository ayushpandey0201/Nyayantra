"""Phase 3: chunk every unit and embed it with BAAI/bge-m3 (dense + sparse), locally.

Chunking (structure-aware):
  * one chunk per unit (section, rule, schedule, judgment paragraph);
  * a unit over MAX_WORDS is split at sub-section / clause / blank-line boundaries into pieces of
    about TARGET_WORDS, and each piece repeats the unit's context header;
  * omitted units ("Omitted by Act X") are not embedded; they stay findable by citation.
Each chunk's embed_text = context header + text, so a bare "(2) ..." still says which Act and section.

Resumable: chunks whose text and model are unchanged are skipped, so re-running only embeds what is new.
Nothing leaves the machine: the model runs on Apple MPS (or CPU).

Usage: python -m ingest.embed [--limit N] [--batch 8]
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import time
from pathlib import Path

import psycopg

from ingest.load_db import DSN

MODEL = "BAAI/bge-m3"
MAX_WORDS, TARGET_WORDS = 700, 450
MAX_TOKENS = 1024


def header(short_title: str, kind: str, path: str | None, heading: str | None) -> str:
    parts = [short_title]
    if path:
        parts.append(path)
    head = " > ".join(parts)
    if heading and heading not in (path or ""):
        head += f" — {heading}"
    return head


def split_text(text: str) -> list[str]:
    if len(text.split()) <= MAX_WORDS:
        return [text]
    # boundaries: sub-sections "(1)", clauses "(a)", Explanation/Illustration, blank lines
    pieces = re.split(r"\n(?=\(\d+[A-Z]?\)\s|\([a-z]{1,4}\)\s|Explanation|Illustration|\n)", text)
    out, cur = [], []
    for p in pieces:
        words = p.split()
        if cur and len(cur) + len(words) > TARGET_WORDS:
            out.append(" ".join(cur))
            cur = []
        cur.extend(words)
        while len(cur) > MAX_WORDS:  # a single huge piece: hard split with a short overlap
            out.append(" ".join(cur[:TARGET_WORDS]))
            cur = cur[TARGET_WORDS - 40:]
    if cur:
        if out and len(cur) < 80:
            out[-1] += " " + " ".join(cur)
        else:
            out.append(" ".join(cur))
    return out


def build_chunks(cur) -> list[dict]:
    cur.execute("""select u.unit_id, u.text, u.path, u.heading, i.short_title, i.kind
                   from unit u join instrument i using (instrument_id)
                   where not u.omitted order by u.instrument_id, u.sort_key""")
    chunks = []
    for uid, text, path, heading, short, kind in cur.fetchall():
        h = header(short, kind, path, heading)
        for n, piece in enumerate(split_text(text)):
            embed_text = f"{h}\n{piece}"
            chunks.append({"chunk_id": f"{uid}#{n}", "unit_id": uid, "seq": n, "context_header": h,
                           "embed_text": embed_text,
                           "sha": hashlib.sha256(embed_text.encode()).hexdigest()})
    return chunks


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--batch", type=int, default=8)
    args = ap.parse_args()

    with psycopg.connect(DSN) as conn, conn.cursor() as cur:
        chunks = build_chunks(cur)
        # drop chunks of units that no longer exist / changed split
        cur.execute("delete from chunk where not (chunk_id = any(%s))", ([c["chunk_id"] for c in chunks],))
        cur.execute("select chunk_id, embed_text from chunk where dense is not null and embedding_model=%s", (MODEL,))
        done = {cid: hashlib.sha256(t.encode()).hexdigest() for cid, t in cur.fetchall()}
        conn.commit()
        todo = [c for c in chunks if done.get(c["chunk_id"]) != c["sha"]]
        print(f"{len(chunks)} chunks from units; {len(chunks) - len(todo)} already embedded; {len(todo)} to embed"
              + (f" (this run: {args.limit})" if args.limit else ""))
        if args.limit:
            todo = todo[:args.limit]
        if not todo:
            return

        import torch
        from FlagEmbedding import BGEM3FlagModel
        device = "mps" if torch.backends.mps.is_available() else "cpu"
        from huggingface_hub import snapshot_download
        # only the PyTorch weights (the repo also carries ~2 GB of ONNX files we do not use)
        local = snapshot_download(MODEL, allow_patterns=["*.json", "pytorch_model.bin", "colbert_linear.pt",
                                                         "sparse_linear.pt", "sentencepiece.bpe.model"])
        model = BGEM3FlagModel(local, use_fp16=True, devices=[device])
        version = Path(local).name  # Hub commit of the weights used
        print(f"model {MODEL} on {device}")

        todo.sort(key=lambda c: len(c["embed_text"]))  # similar lengths per batch = less padding
        t0, n = time.time(), 0
        step = args.batch * 8
        for i in range(0, len(todo), step):
            group = todo[i:i + step]
            out = model.encode([c["embed_text"] for c in group], batch_size=args.batch, max_length=MAX_TOKENS,
                               return_dense=True, return_sparse=True, return_colbert_vecs=False)
            rows = []
            for c, vec, lex in zip(group, out["dense_vecs"], out["lexical_weights"]):
                sparse = {str(k): round(float(v), 4) for k, v in lex.items()}
                rows.append((c["chunk_id"], c["unit_id"], c["seq"], c["context_header"], c["embed_text"],
                             "[" + ",".join(f"{x:.6f}" for x in vec.tolist()) + "]", json.dumps(sparse),
                             MODEL, str(version)))
            cur.executemany(
                """insert into chunk (chunk_id, unit_id, seq, context_header, embed_text, dense, sparse_terms,
                                      embedding_model, model_version)
                   values (%s,%s,%s,%s,%s,%s::halfvec,%s,%s,%s)
                   on conflict (chunk_id) do update set unit_id=excluded.unit_id, seq=excluded.seq,
                     context_header=excluded.context_header, embed_text=excluded.embed_text,
                     dense=excluded.dense, sparse_terms=excluded.sparse_terms,
                     embedding_model=excluded.embedding_model, model_version=excluded.model_version,
                     ingested_at=now()""", rows)
            conn.commit()
            n += len(group)
            rate = n / (time.time() - t0)
            print(f"  {n}/{len(todo)}  {rate:.1f} chunks/s  eta {(len(todo) - n) / rate / 60:.1f} min", flush=True)
        cur.execute("analyze chunk")
        conn.commit()
        print(f"done: {n} chunks in {(time.time() - t0) / 60:.1f} min")


if __name__ == "__main__":
    main()
