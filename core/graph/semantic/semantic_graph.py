"""
Semantic Knowledge Graph — KuzuDB + sentence-transformers

SQLite kg_nodes/kg_edges 를 KuzuDB 에 미러링하여 시맨틱 검색을 지원한다.
connectomeLLM_AGA 의 LongTermMemory 패턴을 Knowledge Graph 에 맞게 포팅.

DB 위치: {db_root}/semantic_graph  (KuzuDB embedded, 파일 기반)
임베딩 모델: paraphrase-multilingual-MiniLM-L12-v2  (한국어/영어 다국어 지원)

동시성: kuzu.Connection 하나를 여러 스레드가 공유하는 대신, KuzuDB 자신이 지원하는
kuzu.AsyncConnection(Connection 풀 + asyncio 디스패치)을 사용한다. 이 클래스의 공개
메서드는 전부 코루틴이며, 이벤트 루프 안에서는 반드시 await 하고, 루프가 없는 평범한
스레드(threading.Thread, ThreadPoolExecutor 워커, 독립 스크립트)에서는 모듈 레벨
run_sg_coro() 로 감싸서 실행한다.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from core.config.runtime_config import get_cfg_value, get_db_root_dir

logger = logging.getLogger(__name__)

# ── KuzuDB 스키마 ─────────────────────────────────────────────────────────────

SCHEMA_DDL = """
CREATE NODE TABLE IF NOT EXISTS KGNode (
    id           STRING PRIMARY KEY,
    title        STRING,
    type         STRING,
    tags         STRING,
    summary      STRING,
    embedding    STRING,
    content_hash STRING,
    updated_at   STRING
);
CREATE REL TABLE IF NOT EXISTS KG_EDGE (
    FROM KGNode TO KGNode,
    rel_type STRING,
    weight   DOUBLE
);
CREATE NODE TABLE IF NOT EXISTS EpisodeNode (
    id         STRING PRIMARY KEY,
    content    STRING,
    keywords   STRING,
    session_id STRING,
    embedding  STRING,
    created_at STRING
);
CREATE REL TABLE IF NOT EXISTS EP_TO_KG (
    FROM EpisodeNode TO KGNode,
    rel_type STRING
);
"""

# 기존 DB에 content_hash 컬럼이 없을 경우 마이그레이션
MIGRATION_DDL = "ALTER TABLE KGNode ADD content_hash STRING DEFAULT ''"


def _now_iso() -> str:
    return datetime.now(tz=timezone.utc).isoformat()


def _content_hash(title: str, summary: str, tags: str) -> str:
    """노드 콘텐츠의 sha256 앞 16자 — 변경 감지용"""
    raw = f"{title}|{summary}|{tags}"
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


def run_sg_coro(coro):
    """이벤트 루프가 없는 평범한 스레드(threading.Thread, ThreadPoolExecutor 워커,
    독립 스크립트)에서 SemanticGraph 코루틴을 실행한다.

    MCP 서버 자체의 이벤트 루프 안에서는 절대 쓰지 말 것 — 이미 루프가 도는 스레드에서
    asyncio.run()을 부르면 RuntimeError가 난다. 그런 곳에서는 그냥 await 할 것.
    """
    return asyncio.run(coro)


# ── SemanticGraph 클래스 ───────────────────────────────────────────────────────


class SemanticGraph:
    """
    KuzuDB 기반 시맨틱 지식 그래프.

    kuzu 또는 sentence-transformers 가 없으면 _enabled=False 로 graceful degradation.
    SQLite kg_nodes/kg_edges 의 시맨틱 레이어 역할.
    """

    _enabled: bool

    def __init__(
        self,
        db_path: str | None = None,
        embedding_model: str = "paraphrase-multilingual-MiniLM-L12-v2",
        read_only: bool = False,
    ) -> None:
        self._write_lock = asyncio.Lock()
        self._sync_lock = asyncio.Lock()  # sync_from_kg 동시 실행 방지
        self._encoder_lock = asyncio.Lock()  # 임베딩 모델 lazy-load 이중 실행 방지
        self._read_only = read_only
        self._enabled = False
        self._embedding_model_name = embedding_model
        self._encoder: Any = None
        self.db: Any = None
        self.async_conn: Any = None

        # In-memory embedding cache — rebuilt lazily; invalidated on upsert_node
        self._cache_dirty: bool = True
        self._cache_ids: list[str] = []
        self._cache_titles: list[str] = []
        self._cache_types: list[str] = []
        self._cache_summaries: list[str] = []
        self._cache_matrix: Any = None  # np.ndarray (N, D) | None

        # Episode embedding cache — separate from KGNode cache
        self._episode_cache_dirty: bool = True
        self._episode_cache_ids: list[str] = []
        self._episode_cache_contents: list[str] = []
        self._episode_cache_dates: list[str] = []
        self._episode_cache_matrix: Any = None  # np.ndarray (N, D) | None

        if db_path is None:
            try:
                db_path = str(Path(get_db_root_dir()) / "semantic_graph")
            except Exception:
                db_path = "semantic_graph"

        self.db_path = db_path

        # overlay.exe 컨텍스트: KuzuDB는 MCP 서버가 독점 소유.
        # ENGRAM_RUNTIME_ROLE=overlay 가 설정된 경우 KuzuDB 열기를 스킵한다.
        if os.environ.get("ENGRAM_RUNTIME_ROLE") == "overlay":
            logger.info("SemanticGraph: overlay 컨텍스트 — KuzuDB 스킵 (MCP 서버 독점)")
            return

        try:
            import kuzu

            db_dir = Path(db_path)
            # read_only 모드에서는 반드시 기존 DB가 있어야 함.
            # 없으면 빈 락 파일만 생성되는 KuzuDB 버그를 방지.
            if read_only and not db_dir.exists():
                logger.info("SemanticGraph(read_only): DB 없음 — 스킵 (%s)", db_path)
                return
            db_dir.parent.mkdir(parents=True, exist_ok=True)
            self.db = kuzu.Database(db_path, read_only=read_only)

            max_concurrent = 4
            try:
                max_concurrent = int(get_cfg_value("semantic_graph.max_concurrent_queries", 4) or 4)
            except Exception:
                pass
            self.async_conn = kuzu.AsyncConnection(self.db, max_concurrent_queries=max_concurrent)

            if not read_only:
                # 스키마 DDL은 이벤트 루프가 보장되지 않는 생성 시점에 실행되므로,
                # AsyncConnection이 "임시 동기 호출용"으로 공식 제공하는
                # acquire_connection()/release_connection()을 그대로 쓴다.
                init_conn = self.async_conn.acquire_connection()
                try:
                    self._init_schema(init_conn)
                finally:
                    self.async_conn.release_connection(init_conn)
            self._enabled = True
            mode = "read-only" if read_only else "read-write"
            logger.info("SemanticGraph initialised at %s (%s)", db_path, mode)
        except ImportError:
            logger.warning("kuzu 미설치 — SemanticGraph 비활성화. `pip install kuzu`")
        except Exception as exc:
            logger.warning("SemanticGraph 초기화 실패 (%s) — 비활성화", exc)

    # ── 스키마 초기화 ─────────────────────────────────────────────────────────

    def _init_schema(self, conn: Any) -> None:
        """생성 시점에만 호출 — 아직 어떤 동시 접근도 불가능한 구간이라 동기 conn 사용."""
        for stmt in SCHEMA_DDL.strip().split(";"):
            stmt = stmt.strip()
            if stmt:
                try:
                    conn.execute(stmt + ";")
                except Exception:
                    logger.debug("_init_schema: DDL 스킵 (already exists): %s", stmt[:60])
        # 기존 DB에 content_hash 컬럼이 없으면 마이그레이션
        try:
            conn.execute(MIGRATION_DDL + ";")
            logger.info("KGNode.content_hash 컬럼 추가 (마이그레이션)")
        except Exception:
            pass  # 이미 존재하면 정상

    # ── 임베딩 캐시 헬퍼 ──────────────────────────────────────────────────────

    def _invalidate_cache(self) -> None:
        self._cache_dirty = True

    async def _rebuild_cache(self) -> None:
        """모든 KGNode 임베딩을 numpy matrix로 로드 (다음 검색 때 한 번만 실행).
        호출자가 이미 self._write_lock을 쥔 상태에서만 호출할 것."""
        try:
            import numpy as np
        except ImportError:
            return

        ids: list[str] = []
        titles: list[str] = []
        types: list[str] = []
        summaries: list[str] = []
        vecs: list[Any] = []

        try:
            res = await self.async_conn.execute("MATCH (n:KGNode) WHERE n.embedding <> '' " "RETURN n.id, n.title, n.type, n.summary, n.embedding")
            while res.has_next():
                row = res.get_next()
                try:
                    vec = np.array(json.loads(row[4]), dtype=np.float32)
                    ids.append(row[0])
                    titles.append(row[1] or "")
                    types.append(row[2] or "")
                    summaries.append(row[3] or "")
                    vecs.append(vec)
                except Exception:
                    pass
        except Exception as exc:
            logger.debug("_rebuild_cache 실패: %s", exc)

        self._cache_matrix = np.stack(vecs) if vecs else None
        self._cache_ids = ids
        self._cache_titles = titles
        self._cache_types = types
        self._cache_summaries = summaries
        self._cache_dirty = False
        logger.debug("_rebuild_cache: %d 노드 로드", len(ids))

    async def _ensure_cache_locked(self) -> None:
        """호출자가 이미 self._write_lock을 쥔 상태에서만 호출."""
        if self._cache_dirty:
            await self._rebuild_cache()

    async def _ensure_cache(self) -> None:
        """락을 스스로 잡는 공개 wrapper — 외부(dev 스크립트 등)에서 직접 캐시를
        갱신하고 싶을 때만 사용. 클래스 내부의 이미 락을 쥔 경로에서는
        _ensure_cache_locked()를 직접 호출할 것(안 그러면 데드락)."""
        async with self._write_lock:
            await self._ensure_cache_locked()

    # ── 임베딩 헬퍼 ───────────────────────────────────────────────────────────

    def _load_encoder_blocking(self) -> None:
        """실제 모델 로드 — 블로킹, asyncio.to_thread로만 호출할 것."""
        try:
            from sentence_transformers import SentenceTransformer
            import transformers as _tr

            prev = _tr.logging.get_verbosity()
            _tr.logging.set_verbosity_error()

            # frozen 번들(통짜 installer)에 동봉된 오프라인 모델을 최우선으로 사용한다.
            # → HuggingFace 서버 연결 없이 로드(설치·구동 시 네트워크 불필요).
            model_ref = self._embedding_model_name
            import sys as _sys
            if getattr(_sys, "frozen", False):
                from pathlib import Path as _Path
                _bundled = _Path(getattr(_sys, "_MEIPASS", "")) / "resource" / "embedding-model"
                if (_bundled / "config.json").exists():
                    model_ref = str(_bundled)

            try:
                try:
                    self._encoder = SentenceTransformer(model_ref, local_files_only=True, device="cpu")
                    logger.info("임베딩 모델 로컬 로드: %s", model_ref)
                except Exception:
                    logger.info("Hub에서 다운로드: %s", self._embedding_model_name)
                    self._encoder = SentenceTransformer(self._embedding_model_name, device="cpu")
            finally:
                _tr.logging.set_verbosity(prev)
        except Exception as exc:
            logger.warning("임베딩 모델 로드 실패: %s", exc)
            self._encoder = False  # 재시도 방지 sentinel

    async def _get_encoder(self) -> Any:
        if self._encoder is None:
            async with self._encoder_lock:
                if self._encoder is None:  # 락 획득 사이 다른 코루틴이 이미 로드했을 수 있음
                    await asyncio.to_thread(self._load_encoder_blocking)
        return self._encoder if self._encoder is not False else None

    async def compute_embedding(self, text: str) -> list[float]:
        """텍스트를 384-dim 정규화 벡터로 변환. 실패 시 빈 리스트 반환."""
        enc = await self._get_encoder()
        if enc is None:
            return []
        try:
            vec = await asyncio.to_thread(enc.encode, text.strip()[:512], normalize_embeddings=True, show_progress_bar=False)
            return vec.tolist()
        except Exception as exc:
            logger.warning("compute_embedding 실패: %s", exc)
            return []

    # ── 노드 upsert ───────────────────────────────────────────────────────────

    def _node_text(self, title: str, summary: str, tags: list | str) -> str:
        """임베딩할 텍스트 구성 (title + summary + tags)"""
        if isinstance(tags, list):
            tags_str = " ".join(tags)
        else:
            try:
                tags_str = " ".join(json.loads(tags))
            except Exception:
                tags_str = str(tags)
        return f"{title}. {summary} {tags_str}".strip()

    async def upsert_node(
        self,
        node_id: str,
        title: str,
        node_type: str,
        tags: list | str,
        summary: str,
        force_reembed: bool = False,
    ) -> bool:
        """노드를 KuzuDB에 upsert. content_hash가 바뀐 경우에만 임베딩 재계산.
        Returns True if embedding was (re)computed, False if reused."""
        async with self._write_lock:
            if not self._enabled:
                return False
            now = _now_iso()
            tags_str = json.dumps(tags, ensure_ascii=False) if isinstance(tags, list) else tags
            new_hash = _content_hash(title, summary, tags_str)

            # 기존 노드의 hash + embedding 조회
            old_hash = ""
            old_emb = ""
            try:
                res = await self.async_conn.execute(
                    "MATCH (n:KGNode {id: $id}) RETURN n.content_hash, n.embedding",
                    {"id": node_id},
                )
                if res.has_next():
                    row = res.get_next()
                    old_hash = row[0] or ""
                    old_emb = row[1] or ""
            except Exception:
                pass

            recomputed = False
            if force_reembed or old_hash != new_hash or not old_emb:
                emb = await self.compute_embedding(self._node_text(title, summary, tags))
                emb_str = json.dumps(emb) if emb else ""
                recomputed = True
            else:
                emb_str = old_emb

            try:
                await self.async_conn.execute(
                    "MERGE (n:KGNode {id: $id}) "
                    "ON CREATE SET n.title=$title, n.type=$type, n.tags=$tags, "
                    "n.summary=$summary, n.embedding=$emb, n.content_hash=$hash, n.updated_at=$now "
                    "ON MATCH SET n.title=$title, n.type=$type, n.tags=$tags, "
                    "n.summary=$summary, n.embedding=$emb, n.content_hash=$hash, n.updated_at=$now",
                    {
                        "id": node_id,
                        "title": title,
                        "type": node_type,
                        "tags": tags_str,
                        "summary": summary,
                        "emb": emb_str,
                        "hash": new_hash,
                        "now": now,
                    },
                )
            except Exception as exc:
                logger.debug("upsert_node 실패 (id=%s): %s", node_id, exc)
            else:
                self._invalidate_cache()
            return recomputed

    # ── 엣지 관리 ────────────────────────────────────────────────────────────

    async def clear_edges(self) -> None:
        async with self._write_lock:
            if not self._enabled:
                return
            try:
                await self.async_conn.execute("MATCH ()-[e:KG_EDGE]->() DELETE e")
            except Exception as exc:
                logger.debug("clear_edges 실패: %s", exc)

    async def create_edge(self, from_id: str, to_id: str, rel_type: str = "links", weight: float = 1.0) -> None:
        async with self._write_lock:
            if not self._enabled:
                return
            try:
                await self.async_conn.execute(
                    "MATCH (a:KGNode {id: $fid}), (b:KGNode {id: $tid}) "
                    "MERGE (a)-[e:KG_EDGE {rel_type: $rel}]->(b) "
                    "ON CREATE SET e.weight=$w "
                    "ON MATCH SET e.weight=$w",
                    {"fid": from_id, "tid": to_id, "rel": rel_type, "w": weight},
                )
            except Exception as exc:
                logger.debug("create_edge 스킵 (%s→%s): %s", from_id, to_id, exc)

    # ── SQLite KG → KuzuDB 동기화 ────────────────────────────────────────────

    async def sync_from_kg(self) -> dict:
        """
        SQLite kg_nodes / kg_edges 를 KuzuDB 에 동기화한다.
        content_hash 기반으로 변경된 노드만 임베딩을 재계산한다.
        _sync_lock 으로 동시 실행을 방지한다 (edge 중복 증폭 방지).
        """
        if not self._enabled:
            return {"status": "disabled"}

        # locked() 체크와 락 진입 사이에 await가 없어야 다른 코루틴이 끼어들 수 없다 —
        # 이 두 줄 사이에 절대 await를 넣지 말 것.
        if self._sync_lock.locked():
            logger.debug("sync_from_kg: 이미 실행 중 — 스킵")
            return {"status": "skipped"}

        async with self._sync_lock:
            from core.storage.db import get_connection

            conn = get_connection()

            nodes = conn.execute("SELECT id, title, type, tags, summary FROM kg_nodes").fetchall()
            node_synced = 0
            reembedded = 0
            for row in nodes:
                nid, title, ntype, tags_json, summary = row
                did_reembed = await self.upsert_node(
                    node_id=nid,
                    title=title,
                    node_type=ntype,
                    tags=tags_json,
                    summary=summary,
                )
                node_synced += 1
                if did_reembed:
                    reembedded += 1

            # 엣지 동기화 (전체 재생성 — clear+create_edge 는 MERGE로 중복 방지됨)
            await self.clear_edges()
            edges = conn.execute("SELECT from_id, to_id, rel_type, weight FROM kg_edges").fetchall()
            edge_synced = 0
            for row in edges:
                await self.create_edge(row[0], row[1], row[2], float(row[3]))
                edge_synced += 1

            conn.close()
            logger.info("SemanticGraph 동기화: nodes=%d (재임베딩=%d), edges=%d", node_synced, reembedded, edge_synced)
            return {"status": "ok", "nodes": node_synced, "reembedded": reembedded, "edges": edge_synced}

    # ── 시맨틱 검색 ───────────────────────────────────────────────────────────

    async def semantic_search(
        self,
        query: str,
        top_k: int = 5,
        threshold: float = 0.30,
        query_vec: list | None = None,
    ) -> list[dict]:
        """
        쿼리 임베딩 → 배치 matmul → top-k 노드 반환 (캐시 기반).
        (임베딩이 정규화돼 있으므로 내적 = 코사인 유사도)
        query_vec이 제공되면 compute_embedding을 건너뛴다.
        """
        if not self._enabled:
            return []
        if query_vec is None:
            query_vec = await self.compute_embedding(query)
        if not query_vec:
            return []
        async with self._write_lock:
            return await self._semantic_search_locked(query_vec, top_k, threshold)

    async def _semantic_search_locked(self, query_vec: list, top_k: int, threshold: float) -> list[dict]:
        """호출자가 이미 self._write_lock을 쥔 상태에서만 호출(link_episode_to_kg 등)."""
        try:
            import numpy as np

            await self._ensure_cache_locked()
            if self._cache_matrix is None:
                return []
            q = np.array(query_vec, dtype=np.float32)
            scores = self._cache_matrix @ q  # (N,) 배치 matmul
            rows: list[dict] = []
            for i, score in enumerate(scores.tolist()):
                if score >= threshold:
                    rows.append(
                        {
                            "id": self._cache_ids[i],
                            "title": self._cache_titles[i],
                            "type": self._cache_types[i],
                            "summary": self._cache_summaries[i],
                            "score": round(score, 4),
                        }
                    )
            rows.sort(key=lambda x: x["score"], reverse=True)
            return rows[:top_k]
        except Exception as exc:
            logger.warning("semantic_search 실패: %s", exc)
            return []

    # ── 특정 노드의 시맨틱 이웃 ──────────────────────────────────────────────

    async def semantic_neighbors(self, node_id: str, top_k: int = 5) -> list[dict]:
        """특정 노드와 의미적으로 가장 유사한 노드를 반환 (캐시 기반)."""
        if not self._enabled:
            return []
        async with self._write_lock:
            try:
                await self._ensure_cache_locked()
                if self._cache_matrix is None or node_id not in self._cache_ids:
                    return []

                import numpy as np

                idx = self._cache_ids.index(node_id)
                q = self._cache_matrix[idx]  # (D,) — no DB round-trip needed
                scores = self._cache_matrix @ q  # (N,)
                results: list[dict] = []
                for i, score in enumerate(scores.tolist()):
                    if i == idx:
                        continue
                    results.append(
                        {
                            "id": self._cache_ids[i],
                            "title": self._cache_titles[i],
                            "type": self._cache_types[i],
                            "summary": self._cache_summaries[i],
                            "score": round(score, 4),
                        }
                    )
                results.sort(key=lambda x: x["score"], reverse=True)
                return results[:top_k]
            except Exception as exc:
                logger.warning("semantic_neighbors 실패: %s", exc)
                return []

    # ── EpisodeNode upsert & 검색 ────────────────────────────────────────────

    async def upsert_episode(
        self,
        episode_id: str,
        content: str,
        keywords: str = "",
        session_id: str = "",
        created_at: str = "",
    ) -> bool:
        """에피소드를 KuzuDB EpisodeNode에 upsert. 임베딩은 content 기반으로 계산.
        Returns True on success, False on failure."""
        async with self._write_lock:
            return await self._upsert_episode_locked(episode_id, content, keywords, session_id, created_at)

    async def _upsert_episode_locked(
        self,
        episode_id: str,
        content: str,
        keywords: str = "",
        session_id: str = "",
        created_at: str = "",
    ) -> bool:
        if not self._enabled:
            return False
        if not created_at:
            created_at = _now_iso()
        emb = await self.compute_embedding(content.strip()[:512])
        emb_str = json.dumps(emb) if emb else ""
        try:
            await self.async_conn.execute(
                "MERGE (e:EpisodeNode {id: $id}) "
                "ON CREATE SET e.content=$content, e.keywords=$keywords, "
                "e.session_id=$session_id, e.embedding=$emb, e.created_at=$created_at "
                "ON MATCH SET e.content=$content, e.keywords=$keywords, "
                "e.session_id=$session_id, e.embedding=$emb, e.created_at=$created_at",
                {
                    "id": episode_id,
                    "content": content,
                    "keywords": keywords,
                    "session_id": session_id,
                    "emb": emb_str,
                    "created_at": created_at,
                },
            )
            self._episode_cache_dirty = True
            # EP_TO_KG 자동 연결 (임베딩 계산 완료 후 즉시)
            if emb:
                await self._link_episode_to_kg_locked(
                    episode_id=episode_id,
                    episode_vec=emb,
                    episode_keywords=keywords,
                )
            return True
        except Exception as exc:
            logger.debug("upsert_episode 실패 (id=%s): %s", episode_id, exc)
            return False

    async def link_episode_to_kg(
        self,
        episode_id: str,
        episode_vec: list | None = None,
        episode_keywords: str = "",
        top_k: int = 3,
        sem_threshold: float = 0.40,
        kw_threshold: int = 1,
        kg_keyword_cache: list[tuple] | None = None,
    ) -> int:
        """EpisodeNode를 관련 KGNode들에 EP_TO_KG 릴레이션으로 연결.

        연결 기준:
        1. 시맨틱 유사도 >= sem_threshold 인 KGNode (rel_type='semantic')
        2. keywords 겹침 >= kw_threshold 단어 (rel_type='keyword')

        Returns: 생성된 릴레이션 수
        """
        async with self._write_lock:
            return await self._link_episode_to_kg_locked(
                episode_id, episode_vec, episode_keywords, top_k, sem_threshold, kw_threshold, kg_keyword_cache
            )

    async def _link_episode_to_kg_locked(
        self,
        episode_id: str,
        episode_vec: list | None = None,
        episode_keywords: str = "",
        top_k: int = 3,
        sem_threshold: float = 0.40,
        kw_threshold: int = 1,
        kg_keyword_cache: list[tuple] | None = None,
    ) -> int:
        """호출자가 이미 self._write_lock을 쥔 상태에서만 호출
        (upsert_episode/sync_all_ep_to_kg 내부)."""
        if not self._enabled:
            return 0

        created = 0

        # 기존 EP_TO_KG 삭제 (rel_type='semantic' 또는 'keyword' 만 대상, 다른 타입 보존)
        try:
            await self.async_conn.execute(
                "MATCH (e:EpisodeNode {id: $eid})-[r:EP_TO_KG]->() "
                "WHERE r.rel_type IN ['semantic', 'keyword'] DELETE r",
                {"eid": episode_id}
            )
        except Exception as exc:
            logger.debug("EP_TO_KG 기존 edge 삭제 실패 (id=%s): %s", episode_id, exc)

        # 1. 시맨틱 연결
        if episode_vec is not None:
            sem_hits = await self._semantic_search_locked(episode_vec, top_k, sem_threshold)
            for hit in sem_hits:
                try:
                    await self.async_conn.execute(
                        "MERGE (e:EpisodeNode {id: $eid}) " "MERGE (k:KGNode {id: $kid}) " "MERGE (e)-[r:EP_TO_KG {rel_type: 'semantic'}]->(k)",
                        {"eid": episode_id, "kid": hit["id"]},
                    )
                    created += 1
                except Exception as exc:
                    logger.debug("EP_TO_KG semantic link 실패 (%s→%s): %s", episode_id, hit["id"], exc)

        # 2. 키워드 연결 (정규화된 테이블 활용)
        try:
            # SQLite에서 현재 에피소드의 정규화된 키워드 목록 가져오기
            from core.storage.db import get_connection
            sqlite_conn = get_connection()
            ep_keywords_rows = sqlite_conn.execute(
                "SELECT k.name FROM keywords k "
                "JOIN memory_keywords mk ON k.id = mk.keyword_id "
                "WHERE mk.memory_id = ?",
                (episode_id,)
            ).fetchall()
            ep_words = set(row["name"] for row in ep_keywords_rows)
            sqlite_conn.close()

            if ep_words:
                rows_to_scan = kg_keyword_cache
                if rows_to_scan is None:
                    res = await self.async_conn.execute("MATCH (k:KGNode) WHERE k.tags <> '' OR k.title <> '' RETURN k.id, k.tags, k.title")
                    rows_to_scan = []
                    while res.has_next():
                        rows_to_scan.append(res.get_next())

                for row in rows_to_scan:
                    kg_id, tags_raw, title = row[0], row[1] or "", row[2] or ""
                    # KGNode의 태그와 제목에서 키워드 추출
                    kg_words = set(w.lower() for w in (tags_raw + " " + title).replace(",", " ").split() if len(w) > 1)

                    # 교집합 크기 계산
                    intersection = ep_words & kg_words
                    if len(intersection) >= kw_threshold:
                        await self.async_conn.execute(
                            "MERGE (e:EpisodeNode {id: $eid}) "
                            "MERGE (k:KGNode {id: $kid}) "
                            "MERGE (e)-[r:EP_TO_KG {rel_type: 'keyword'}]->(k) "
                            "SET r.weight = $weight, r.keywords = $matched",
                            {
                                "eid": episode_id,
                                "kid": kg_id,
                                "weight": len(intersection),
                                "matched": ", ".join(list(intersection))
                            },
                        )
                        created += 1
        except Exception as exc:
            logger.debug("EP_TO_KG keyword link (normalized) 실패: %s", exc)

        if created:
            logger.debug("EP_TO_KG: episode=%s, %d 릴레이션 생성", episode_id, created)
        return created

    async def sync_all_ep_to_kg(self, sem_threshold: float = 0.40, top_k: int = 3) -> dict:
        """기존 EpisodeNode 전체에 link_episode_to_kg 소급 적용.
        MCP 서버 중단 후 실행해야 함 (KuzuDB 단일 writer 제약).
        Returns: {"processed": N, "linked": M}
        """
        async with self._write_lock:
            return await self._sync_all_ep_to_kg_locked(sem_threshold, top_k)

    async def _sync_all_ep_to_kg_locked(self, sem_threshold: float = 0.40, top_k: int = 3) -> dict:
        if not self._enabled:
            return {"processed": 0, "linked": 0, "error": "KuzuDB disabled"}

        if self._episode_cache_dirty:
            await self._rebuild_episode_cache_locked()

        # KGNode keyword/title 캐시 1회 빌드 → link_episode_to_kg 호출마다 전체 스캔 방지
        kg_keyword_cache: list[tuple] | None = None
        try:
            res = await self.async_conn.execute("MATCH (k:KGNode) WHERE k.tags <> '' RETURN k.id, k.tags, k.title")
            kg_keyword_cache = []
            while res.has_next():
                kg_keyword_cache.append(res.get_next())
        except Exception:
            kg_keyword_cache = None

        processed = 0
        linked = 0
        for i, ep_id in enumerate(self._episode_cache_ids):
            ep_vec = self._episode_cache_matrix[i].tolist() if self._episode_cache_matrix is not None else None
            keywords = ""
            try:
                res = await self.async_conn.execute("MATCH (e:EpisodeNode {id: $id}) RETURN e.keywords", {"id": ep_id})
                if res.has_next():
                    keywords = res.get_next()[0] or ""
            except Exception:
                pass
            n = await self._link_episode_to_kg_locked(
                ep_id,
                episode_vec=ep_vec,
                episode_keywords=keywords,
                top_k=top_k,
                sem_threshold=sem_threshold,
                kg_keyword_cache=kg_keyword_cache,
            )
            linked += n
            processed += 1

        logger.info("sync_all_ep_to_kg 완료: %d 에피소드, %d 릴레이션 생성", processed, linked)
        return {"processed": processed, "linked": linked}

    async def _rebuild_episode_cache(self) -> None:
        """락을 스스로 잡는 공개 wrapper(외부 dev 스크립트용)."""
        async with self._write_lock:
            await self._rebuild_episode_cache_locked()

    async def _rebuild_episode_cache_locked(self) -> None:
        """EpisodeNode 임베딩 캐시 재빌드. 호출자가 이미 self._write_lock을 쥔 상태에서만."""
        try:
            import numpy as np
        except ImportError:
            return

        ids: list[str] = []
        dates: list[str] = []
        vecs: list[Any] = []

        try:
            # content 제거: id, created_at, embedding만 조회
            res = await self.async_conn.execute(
                "MATCH (e:EpisodeNode) WHERE e.embedding <> '' "
                "RETURN e.id, e.created_at, e.embedding"
            )
            while res.has_next():
                row = res.get_next()
                try:
                    vec = np.array(json.loads(row[2]), dtype=np.float32)
                    ids.append(row[0])
                    dates.append(row[1] or "")
                    vecs.append(vec)
                except Exception:
                    pass
        except Exception as exc:
            logger.debug("_rebuild_episode_cache 실패: %s", exc)

        self._episode_cache_matrix = np.stack(vecs) if vecs else None
        self._episode_cache_ids = ids
        self._episode_cache_dates = dates
        self._episode_cache_dirty = False
        logger.debug("_rebuild_episode_cache: %d 에피소드 로드", len(ids))

    async def episode_semantic_search(
        self,
        query: str,
        top_k: int = 5,
        threshold: float = 0.25,
        max_age_days: int = 0,
        query_vec: list | None = None,
    ) -> list[dict]:
        """에피소드 시맨틱 검색. max_age_days > 0이면 최근 N일 이내만 검색.
        반환 dict: {"id", "content", "score", "created_at"}
        query_vec이 제공되면 compute_embedding을 건너뛴다.
        """
        if not self._enabled:
            return []
        if query_vec is None:
            query_vec = await self.compute_embedding(query)
        if not query_vec:
            return []
        try:
            import numpy as np
            from datetime import datetime, timedelta, timezone

            async with self._write_lock:
                if self._episode_cache_dirty:
                    await self._rebuild_episode_cache_locked()
                if self._episode_cache_matrix is None:
                    return []

                q = np.array(query_vec, dtype=np.float32)
                scores = self._episode_cache_matrix @ q  # (N,) 배치 matmul

                # temporal filter
                cutoff: datetime | None = None
                if max_age_days > 0:
                    cutoff = datetime.now(tz=timezone.utc) - timedelta(days=max_age_days)

                rows: list[dict] = []
                for i, score in enumerate(scores.tolist()):
                    if score < threshold:
                        continue
                    if cutoff is not None:
                        date_str = self._episode_cache_dates[i]
                        if date_str:
                            try:
                                ep_dt = datetime.fromisoformat(date_str.replace("Z", "+00:00"))
                                if ep_dt.tzinfo is None:
                                    ep_dt = ep_dt.replace(tzinfo=timezone.utc)
                                if ep_dt < cutoff:
                                    continue
                            except Exception:
                                pass  # 파싱 실패 시 포함
                    rows.append(
                        {
                            "id": self._episode_cache_ids[i],
                            "content": "",
                            "score": round(score, 4),
                            "created_at": self._episode_cache_dates[i],
                        }
                    )
                rows.sort(key=lambda x: x["score"], reverse=True)
                rows = rows[:top_k]

            # 락 밖: 이후는 id 기준 개별 조회라 캐시 배열(TOCTOU 대상)과 무관
            if rows:
                top_ids = [r["id"] for r in rows]
                content_map: dict[str, str] = {}
                try:
                    for ep_id in top_ids:
                        res2 = await self.async_conn.execute(
                            "MATCH (e:EpisodeNode {id: $id}) RETURN e.content",
                            {"id": ep_id},
                        )
                        if res2.has_next():
                            content_map[ep_id] = res2.get_next()[0] or ""
                except Exception:
                    pass
                for r in rows:
                    r["content"] = content_map.get(r["id"], "")

            return rows
        except Exception as exc:
            logger.warning("episode_semantic_search 실패: %s", exc)
            return []

    # ── Cypher 직접 쿼리 ─────────────────────────────────────────────────────

    async def cypher_query(self, cypher: str, params: dict | None = None) -> list[dict]:
        """KuzuDB에 직접 Cypher 쿼리를 실행한다. 결과는 list of dict로 반환.
        필터 있는 DELETE/MERGE 등 쓰기도 허용되므로(mcp_server._is_dangerous_cypher 참조)
        쓰기 경로로 취급해 락을 잡고, 실행 후 캐시를 무효화한다."""
        if not self._enabled:
            return []
        async with self._write_lock:
            try:
                res = await self.async_conn.execute(cypher, params or {})
                results = []
                col_names = res.get_column_names()
                while res.has_next():
                    row = res.get_next()
                    results.append(dict(zip(col_names, row)))
                # 임의 Cypher가 KGNode/EpisodeNode 어느 쪽이든 건드렸을 수 있어 둘 다 무효화
                self._invalidate_cache()
                self._episode_cache_dirty = True
                return results
            except Exception as exc:
                logger.warning("cypher_query 실패: %s", exc)
                return [{"error": str(exc)}]

    async def count(self, cypher: str) -> int:
        """`RETURN count(...)` 형태의 스칼라 집계 전용 헬퍼(예: /api/sg/stats).
        cypher_query처럼 컬럼명에 의존하지 않고 첫 행 첫 컬럼을 그대로 반환한다."""
        if not self._enabled:
            return 0
        async with self._write_lock:
            try:
                res = await self.async_conn.execute(cypher)
                return int(res.get_next()[0]) if res.has_next() else 0
            except Exception as exc:
                logger.warning("count 쿼리 실패: %s", exc)
                return 0

    @property
    def enabled(self) -> bool:
        return self._enabled


# ── 싱글턴 ────────────────────────────────────────────────────────────────────

_sg_instance: SemanticGraph | None = None
_sg_instance_lock = threading.Lock()  # 최초 생성 레이스 방지. 루프 없는 스레드에서도
                                       # 불릴 수 있어 asyncio.Lock이 아닌 실제 스레드 락 사용.


def get_semantic_graph() -> SemanticGraph:
    global _sg_instance
    if _sg_instance is None:
        with _sg_instance_lock:
            if _sg_instance is None:
                _sg_instance = SemanticGraph()
    return _sg_instance
