"""Runtime settings. Secrets come from .env (git-ignored) and are never logged."""
from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
load_dotenv(ROOT / ".env")

DSN = os.environ.get("NYAYANTRA_DSN", "host=/tmp dbname=nyayantra")

EMBED_MODEL = "BAAI/bge-m3"
EMBED_FILES = ["*.json", "pytorch_model.bin", "colbert_linear.pt", "sparse_linear.pt", "sentencepiece.bpe.model"]
RERANK_MODEL = "BAAI/bge-reranker-v2-m3"
RERANK_FILES = ["*.json", "model.safetensors", "sentencepiece.bpe.model"]

# Retrieval defaults (tuned on eval/retrieval_gold.yaml; see eval/results/retrieval.md)
CANDIDATES = 50          # per retriever, in chunks
RRF_K = 60               # reciprocal rank fusion constant (Cormack et al., 2009)
RERANK_POOL = int(os.environ.get("NYAYANTRA_RERANK_POOL", 10))        # fused units scored by the cross-encoder
RERANK_MAX_TOKENS = int(os.environ.get("NYAYANTRA_RERANK_TOKENS", 256))  # query + passage, truncated
# On an 8 GB M1 the cross-encoder costs ~0.35 s per passage at 256 tokens, so the pool is kept small.
MAX_PER_JUDGMENT = 2     # paragraphs of one judgment in the final top k
