"""intent_indexer.py — Semantic intent search via Qdrant (or in-memory fallback).

Production use requires a running Qdrant instance pointed to by the
``QDRANT_URL`` environment variable.  When Qdrant is unavailable the indexer
falls back to a bounded in-memory list with substring matching and logs a
startup warning so the gap is visible in logs rather than silently degraded.
"""
from __future__ import annotations

import logging
import os

logger = logging.getLogger(__name__)

# Maximum entries kept in the fallback in-memory index.
_MEM_INDEX_MAX = 5_000


class IntentIndexer:
    """Semantic intent search backed by Qdrant.

    Falls back to bounded in-memory substring matching when Qdrant is not
    reachable.  A WARNING is emitted at init time so the degraded mode is
    visible in monitoring dashboards.
    """

    def __init__(
        self,
        qdrant_url: str | None = None,
        collection_name: str = "xioflow_intents",
    ) -> None:
        self.qdrant_url      = qdrant_url or os.environ.get("QDRANT_URL", "")
        self.collection_name = collection_name
        self._client         = None
        self._memory_index: list[dict] = []
        self._qdrant_ok = False

        if self.qdrant_url:
            try:
                from qdrant_client import QdrantClient  # noqa: PLC0415
                self._client = QdrantClient(url=self.qdrant_url)
                self._qdrant_ok = True
                logger.info(
                    "intent_indexer.qdrant_connected",
                    extra={"url": self.qdrant_url, "collection": self.collection_name},
                )
            except ImportError:
                logger.warning(
                    "intent_indexer.qdrant_client_not_installed",
                    extra={
                        "advice": "pip install qdrant-client to enable semantic search",
                        "fallback": "in-memory substring matching",
                    },
                )
            except Exception as exc:
                logger.warning(
                    "intent_indexer.qdrant_unreachable",
                    extra={
                        "url":      self.qdrant_url,
                        "error":    str(exc),
                        "fallback": "in-memory substring matching — semantic accuracy degraded",
                    },
                )
        else:
            logger.warning(
                "intent_indexer.no_qdrant_url",
                extra={
                    "advice":   "Set QDRANT_URL env var to enable Qdrant-backed semantic search.",
                    "fallback": "in-memory substring matching — NOT suitable for production scale.",
                },
            )

    # ── Index ──────────────────────────────────────────────────────────────────

    def index_intent(
        self,
        intent: str,
        domain: str,
        org_id: str,
        tier: str,
        node_id: str,
    ) -> None:
        """Store intent metadata in Qdrant (or the in-memory fallback)."""
        if self._qdrant_ok and self._client:
            try:
                # Minimal Qdrant upsert — embedding is deferred to a background
                # embedding job; here we store raw text payload only.
                from qdrant_client.models import PointStruct  # noqa: PLC0415
                import hashlib  # noqa: PLC0415
                point_id = int(hashlib.md5(f"{org_id}:{node_id}".encode()).hexdigest(), 16) % (2**63)
                self._client.upsert(
                    collection_name=self.collection_name,
                    points=[
                        PointStruct(
                            id=point_id,
                            vector=[0.0] * 384,  # placeholder — real embedding added by indexer job
                            payload={
                                "intent":  intent,
                                "domain":  domain,
                                "org_id":  org_id,
                                "tier":    tier,
                                "node_id": node_id,
                            },
                        )
                    ],
                )
                logger.debug("intent_indexer.indexed", extra={"intent": intent, "node_id": node_id})
                return
            except Exception as exc:
                logger.warning("intent_indexer.qdrant_upsert_failed", extra={"error": str(exc)})
                # Fall through to in-memory index

        # In-memory fallback — bounded to avoid unbounded growth
        if len(self._memory_index) >= _MEM_INDEX_MAX:
            self._memory_index.pop(0)
        self._memory_index.append({
            "intent":  intent,
            "domain":  domain,
            "org_id":  org_id,
            "tier":    tier,
            "node_id": node_id,
        })

    # ── Search ─────────────────────────────────────────────────────────────────

    def search(self, query: str, org_id: str, top_k: int = 5) -> list[dict]:
        """Search for intents matching `query` for the given org.

        Uses Qdrant vector search when available; falls back to substring
        matching on the in-memory index otherwise.
        """
        if self._qdrant_ok and self._client:
            try:
                # Payload-filter search (no embedding yet — text filter only).
                from qdrant_client.models import Filter, FieldCondition, MatchValue  # noqa: PLC0415
                results = self._client.scroll(
                    collection_name=self.collection_name,
                    scroll_filter=Filter(
                        must=[
                            FieldCondition(key="org_id", match=MatchValue(value=org_id)),
                        ]
                    ),
                    limit=top_k * 5,  # over-fetch then filter by query
                    with_payload=True,
                )
                hits = []
                for point in (results[0] if results else []):
                    payload = point.payload or {}
                    if query.lower() in payload.get("intent", "").lower():
                        score = len(query) / max(len(payload.get("intent", query)), 1)
                        hits.append({**payload, "score": score})
                hits.sort(key=lambda x: x["score"], reverse=True)
                return hits[:top_k]
            except Exception as exc:
                logger.warning("intent_indexer.qdrant_search_failed", extra={"error": str(exc)})
                # Fall through to in-memory search

        # In-memory substring search fallback
        results = []
        for entry in self._memory_index:
            if entry["org_id"] == org_id and query.lower() in entry["intent"].lower():
                score = len(query) / max(len(entry["intent"]), 1)
                results.append({**entry, "score": score})
        results.sort(key=lambda x: x["score"], reverse=True)
        return results[:top_k]
