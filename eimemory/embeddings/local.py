from __future__ import annotations

import hashlib
from functools import lru_cache
import math
import re


TOKEN_RE = re.compile(r"[a-z0-9]{2,}", re.IGNORECASE)
VECTOR_SIZE = 128


def embed_text(text: str, *, size: int = VECTOR_SIZE) -> list[float]:
    return list(_embed_text_cached(str(text or ""), int(size or VECTOR_SIZE)))


@lru_cache(maxsize=4096)
def _embed_text_cached(text: str, size: int = VECTOR_SIZE) -> tuple[float, ...]:
    vector = [0.0] * size
    tokens = _tokens(text)
    if not tokens:
        return tuple(vector)
    for token in tokens:
        digest = hashlib.sha1(token.encode("utf-8")).digest()
        bucket = int.from_bytes(digest[:2], "big") % size
        sign = 1.0 if digest[2] % 2 == 0 else -1.0
        weight = 1.0 + min(len(token), 8) / 8.0
        vector[bucket] += sign * weight
    norm = math.sqrt(sum(value * value for value in vector))
    if norm == 0:
        return tuple(vector)
    return tuple(value / norm for value in vector)


def cosine_similarity(left: list[float], right: list[float]) -> float:
    if not left or not right:
        return 0.0
    limit = min(len(left), len(right))
    return sum(left[idx] * right[idx] for idx in range(limit))


def _tokens(text: str) -> list[str]:
    lowered = text.lower()
    tokens = TOKEN_RE.findall(lowered)
    compact = re.sub(r"\s+", "", lowered)
    trigrams = [compact[idx: idx + 3] for idx in range(max(0, len(compact) - 2))]
    return tokens + trigrams
