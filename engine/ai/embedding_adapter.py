"""
engine/ai/embedding_adapter.py
텍스트 임베딩 어댑터.

sentence-transformers 설치 시: all-MiniLM-L6-v2 (384차원, 다국어 지원)
미설치 시: TFIDFHash fallback (128차원, numpy만 필요) — 항상 성공.
"""
from __future__ import annotations

import hashlib
import logging
import math
from typing import Protocol, runtime_checkable

logger = logging.getLogger(__name__)

_provider_instance: "EmbeddingProvider | None" = None


@runtime_checkable
class EmbeddingProvider(Protocol):
    def encode(self, texts: list[str]) -> list[list[float]]: ...
    def dimension(self) -> int: ...


class SentenceTransformerProvider:
    """sentence-transformers 기반 고품질 임베딩. lazy load."""

    MODEL_NAME = "all-MiniLM-L6-v2"

    def __init__(self) -> None:
        self._model = None

    def _load(self) -> None:
        if self._model is None:
            from sentence_transformers import SentenceTransformer
            self._model = SentenceTransformer(self.MODEL_NAME)
            logger.info("embedding_model_loaded model=%s dim=384", self.MODEL_NAME)

    def encode(self, texts: list[str]) -> list[list[float]]:
        self._load()
        return self._model.encode(texts, convert_to_numpy=True).tolist()

    def dimension(self) -> int:
        return 384


class TFIDFHashProvider:
    """
    numpy만으로 동작하는 hash 기반 임베딩 fallback.
    SHA-256 전체 32 바이트 → 16개 버킷에 TF 가중치 분산.
    """

    DIM = 128

    def encode(self, texts: list[str]) -> list[list[float]]:
        import numpy as np
        result = []
        for text in texts:
            tokens = text.lower().split()
            vec = np.zeros(self.DIM, dtype=float)
            if not tokens:
                result.append(vec.tolist())
                continue
            # token frequency
            freq: dict[str, int] = {}
            for t in tokens:
                freq[t] = freq.get(t, 0) + 1
            for token, count in freq.items():
                h = hashlib.sha256(token.encode()).digest()  # 32 bytes
                # 전체 32바이트 활용 → 16개 버킷 (2바이트씩)
                for i in range(0, 32, 2):
                    idx = ((h[i] << 8) | h[i + 1]) % self.DIM
                    vec[idx] += 1.0 + math.log(count)  # sublinear TF
            norm = float(np.linalg.norm(vec))
            if norm > 0:
                vec = vec / norm
            result.append(vec.tolist())
        return result

    def dimension(self) -> int:
        return self.DIM


def get_embedding_provider() -> EmbeddingProvider:
    """프로세스 싱글턴 프로바이더 반환. 최초 호출 시 초기화."""
    global _provider_instance
    if _provider_instance is None:
        try:
            import sentence_transformers  # noqa: F401
            _provider_instance = SentenceTransformerProvider()
            logger.info("embedding_provider=sentence_transformers")
        except ImportError:
            _provider_instance = TFIDFHashProvider()
            logger.info("embedding_provider=tfidf_hash_fallback (sentence-transformers not installed)")
    return _provider_instance


def cosine_similarity(a: list[float], b: list[float]) -> float:
    """두 벡터의 코사인 유사도 (-1 ~ 1). 정규화된 벡터면 dot product와 동일."""
    import numpy as np
    va = np.array(a, dtype=float)
    vb = np.array(b, dtype=float)
    denom = float(np.linalg.norm(va)) * float(np.linalg.norm(vb))
    if denom < 1e-9:
        return 0.0
    return float(np.dot(va, vb) / denom)
