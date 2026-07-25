"""In-process semantic embeddings for the memory system (the RAG layer).

fastembed runs a small ONNX model (BAAI/bge-small-en-v1.5, 384 dims) on CPU:
~4ms per text after warm-up, no API key, no external service, no per-call
cost. At FORMA's scale (hundreds to a few thousand memories per rider) we
store vectors as JSON on the row and rank with numpy cosine in Python —
honest and fast. Upgrade path when a rider passes ~5k memories: move the
column to a pgvector index; the write/read seams here don't change.

The model lazy-loads on first use (~130MB download on a fresh container),
so a startup background task warms it without blocking requests.
"""

import logging
import threading

import numpy as np

logger = logging.getLogger(__name__)

_MODEL_NAME = "BAAI/bge-small-en-v1.5"
_model = None
_model_lock = threading.Lock()
_model_failed = False


def _get_model():
    global _model, _model_failed
    if _model is not None or _model_failed:
        return _model
    with _model_lock:
        if _model is not None or _model_failed:
            return _model
        try:
            from fastembed import TextEmbedding

            _model = TextEmbedding(_MODEL_NAME)
        except Exception:
            # Embeddings are an enhancement, never a dependency: retrieval
            # falls back to recency scoring when the model can't load.
            logger.exception("Embedding model failed to load — semantic retrieval off")
            _model_failed = True
    return _model


def is_available() -> bool:
    return _get_model() is not None


def embed_texts(texts: list[str]) -> list[list[float]] | None:
    """Embed a batch of texts. None if the model is unavailable."""
    model = _get_model()
    if model is None or not texts:
        return None
    try:
        return [v.tolist() for v in model.embed(texts)]
    except Exception:
        logger.exception("Embedding failed for %d texts", len(texts))
        return None


def embed_query(text: str) -> list[float] | None:
    """Embed a retrieval query (bge models prefer a query prefix)."""
    out = embed_texts([f"Represent this sentence for searching relevant passages: {text}"])
    return out[0] if out else None


def cosine_scores(query_vec: list[float], vectors: list[list[float] | None]) -> list[float]:
    """Cosine similarity of query against each vector (0.0 where missing)."""
    q = np.asarray(query_vec, dtype=np.float32)
    qn = np.linalg.norm(q) or 1.0
    scores: list[float] = []
    for v in vectors:
        if not v:
            scores.append(0.0)
            continue
        a = np.asarray(v, dtype=np.float32)
        an = np.linalg.norm(a) or 1.0
        scores.append(float(np.dot(q, a) / (qn * an)))
    return scores
