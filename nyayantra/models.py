"""Local models, loaded once and kept in memory. Nothing is sent off the machine."""
from __future__ import annotations

import os
import threading
from functools import lru_cache

from nyayantra.config import EMBED_FILES, EMBED_MODEL, RERANK_FILES, RERANK_MAX_TOKENS, RERANK_MODEL

_lock = threading.Lock()


def _device() -> str:
    import torch
    return "mps" if torch.backends.mps.is_available() else "cpu"


def _local(repo: str, files: list[str]) -> str:
    from huggingface_hub import snapshot_download
    os.environ.setdefault("HF_HUB_OFFLINE", "1")   # weights are already on disk; never phone home at query time
    return snapshot_download(repo, allow_patterns=files)


@lru_cache(maxsize=1)
def embedder():
    from FlagEmbedding import BGEM3FlagModel
    with _lock:
        return BGEM3FlagModel(_local(EMBED_MODEL, EMBED_FILES), use_fp16=True, devices=[_device()])


@lru_cache(maxsize=1)
def reranker():
    from FlagEmbedding import FlagReranker
    with _lock:
        return FlagReranker(_local(RERANK_MODEL, RERANK_FILES), use_fp16=True, devices=[_device()])


def encode_query(text: str) -> tuple[list[float], dict[str, float]]:
    out = embedder().encode([text], batch_size=1, max_length=512, return_dense=True, return_sparse=True,
                            return_colbert_vecs=False)
    return out["dense_vecs"][0].tolist(), {str(k): float(v) for k, v in out["lexical_weights"][0].items()}


def rerank(query: str, passages: list[str]) -> list[float]:
    if not passages:
        return []
    scores = reranker().compute_score([[query, p] for p in passages], batch_size=len(passages), max_length=RERANK_MAX_TOKENS,
                                       normalize=True)
    return [float(s) for s in (scores if isinstance(scores, list) else [scores])]
