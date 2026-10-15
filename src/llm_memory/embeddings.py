from __future__ import annotations

import hashlib
import json
import math
from datetime import UTC, datetime
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from sqlalchemy import select
from sqlalchemy.orm import Session

from llm_memory.config import Settings
from llm_memory.models import Memory, MemoryEmbedding


class EmbeddingProviderError(RuntimeError):
    pass


def provider_key(settings: Settings) -> str:
    endpoint = hashlib.sha256((settings.embedding_url or "").encode()).hexdigest()[:16]
    return f"{settings.embedding_model}:{endpoint}"


def embed_texts(texts: list[str], settings: Settings) -> list[list[float]]:
    if not settings.embedding_url:
        return []
    request_body = json.dumps({"model": settings.embedding_model, "input": texts}).encode()
    headers = {"Content-Type": "application/json", "Accept": "application/json"}
    if settings.embedding_api_key:
        headers["Authorization"] = f"Bearer {settings.embedding_api_key}"
    request = Request(settings.embedding_url, data=request_body, headers=headers, method="POST")
    try:
        with urlopen(request, timeout=20) as response:
            payload = json.loads(response.read())
    except (HTTPError, URLError, TimeoutError, json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise EmbeddingProviderError("Embedding endpoint request failed") from exc
    try:
        rows = payload["data"]
        ordered = sorted(rows, key=lambda row: row["index"])
        vectors = [[float(value) for value in row["embedding"]] for row in ordered]
    except (KeyError, TypeError, ValueError) as exc:
        raise EmbeddingProviderError("Embedding endpoint returned an invalid response") from exc
    if len(vectors) != len(texts) or any(not vector for vector in vectors):
        raise EmbeddingProviderError("Embedding endpoint returned an unexpected vector count")
    dimensions = len(vectors[0])
    if any(len(vector) != dimensions or any(not math.isfinite(value) for value in vector)
           for vector in vectors):
        raise EmbeddingProviderError("Embedding endpoint returned inconsistent vectors")
    return vectors


def store_embedding(session: Session, memory: Memory, settings: Settings) -> bool:
    if not settings.embedding_url:
        return False
    try:
        vector = embed_texts([memory.content], settings)[0]
    except (EmbeddingProviderError, IndexError):
        return False
    record = session.get(MemoryEmbedding, memory.id)
    if record is None:
        record = MemoryEmbedding(memory_id=memory.id, model=settings.embedding_model, vector="[]")
        session.add(record)
    record.model = provider_key(settings)
    record.vector = json.dumps(vector, separators=(",", ":"))
    record.updated_at = datetime.now(UTC)
    return True


def ensure_embeddings(
    session: Session, memories: list[Memory], settings: Settings
) -> dict[str, list[float]]:
    if not settings.embedding_url or not memories:
        return {}
    ids = [memory.id for memory in memories]
    existing = {
        row.memory_id: row
        for row in session.scalars(
            select(MemoryEmbedding).where(MemoryEmbedding.memory_id.in_(ids))
        ).all()
    }
    missing = [
        memory for memory in memories
        if memory.id not in existing or existing[memory.id].model != provider_key(settings)
    ]
    try:
        for start in range(0, len(missing), 64):
            batch = missing[start : start + 64]
            batch_vectors = embed_texts([memory.content for memory in batch], settings)
            for memory, vector in zip(batch, batch_vectors, strict=True):
                record = existing.get(memory.id)
                if record is None:
                    record = MemoryEmbedding(memory_id=memory.id, model=provider_key(settings), vector="[]")
                    session.add(record)
                record.model = provider_key(settings)
                record.vector = json.dumps(vector, separators=(",", ":"))
                record.updated_at = datetime.now(UTC)
                existing[memory.id] = record
        session.commit()
    except (EmbeddingProviderError, IndexError, ValueError):
        session.rollback()
        return {}
    vectors: dict[str, list[float]] = {}
    for memory_id, record in existing.items():
        if record.model == provider_key(settings):
            try:
                vectors[memory_id] = [float(value) for value in json.loads(record.vector)]
            except (json.JSONDecodeError, TypeError, ValueError):
                continue
    return vectors


def cosine_similarity(left: list[float], right: list[float]) -> float:
    if len(left) != len(right) or not left:
        return 0.0
    left_norm = math.sqrt(sum(value * value for value in left))
    right_norm = math.sqrt(sum(value * value for value in right))
    if not left_norm or not right_norm:
        return 0.0
    return sum(a * b for a, b in zip(left, right, strict=True)) / (left_norm * right_norm)
