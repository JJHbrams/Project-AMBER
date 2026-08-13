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
import re
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, TypedDict

from core.config.runtime_config import get_cfg_value, get_db_root_dir
from core.context.project_scope import resolve_kg_node_id

logger = logging.getLogger(__name__)


class CrossLoopAsyncLock:
    """여러 이벤트 루프에서 공유할 수 있는 비동기 컨텍스트 락."""

    def __init__(self) -> None:
        self._lock = threading.Lock()

    def locked(self) -> bool:
        return self._lock.locked()

    def try_acquire(self) -> bool:
        return self._lock.acquire(blocking=False)

    async def acquire(self) -> None:
        while not self.try_acquire():
            await asyncio.sleep(0.01)

    def release(self) -> None:
        self._lock.release()

    async def __aenter__(self) -> CrossLoopAsyncLock:
        await self.acquire()
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        self.release()


class EpisodeReconciliationReport(TypedDict):
    canonical_count: int
    episode_count_before: int
    stale_ids: list[str]
    missing_ids: list[str]
    unlinked_episode_ids: list[str]
    applied: bool
    deleted_count: int
    episode_count_after: int


def _memory_id_sort_key(value: str) -> tuple[int, int, str]:
    text = str(value)
    if text.isdigit():
        return (0, int(text), text)
    return (1, 0, text)

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
    rel_type  STRING,
    weight    DOUBLE DEFAULT 0.0,
    keywords  STRING DEFAULT '',
    score     DOUBLE DEFAULT 0.0,
    method    STRING DEFAULT '',
    model     STRING DEFAULT '',
    version   STRING DEFAULT '',
    created_at STRING DEFAULT ''
);
"""

# 기존 DB에 content_hash 컬럼이 없을 경우 마이그레이션
MIGRATION_DDL = (
    ("KGNode.content_hash", "ALTER TABLE KGNode ADD content_hash STRING DEFAULT ''"),
    ("EP_TO_KG.weight", "ALTER TABLE EP_TO_KG ADD weight DOUBLE DEFAULT 0.0"),
    ("EP_TO_KG.keywords", "ALTER TABLE EP_TO_KG ADD keywords STRING DEFAULT ''"),
    ("EP_TO_KG.score", "ALTER TABLE EP_TO_KG ADD score DOUBLE DEFAULT 0.0"),
    ("EP_TO_KG.method", "ALTER TABLE EP_TO_KG ADD method STRING DEFAULT ''"),
    ("EP_TO_KG.model", "ALTER TABLE EP_TO_KG ADD model STRING DEFAULT ''"),
    ("EP_TO_KG.version", "ALTER TABLE EP_TO_KG ADD version STRING DEFAULT ''"),
    ("EP_TO_KG.created_at", "ALTER TABLE EP_TO_KG ADD created_at STRING DEFAULT ''"),
)

EP_TO_KG_LINK_VERSION = "1"
EP_TO_KG_KEYWORD_STOPWORDS = {
    "assistant",
    "close",
    "content",
    "date",
    "engram",
    "memory",
    "model",
    "project",
    "provider",
    "save",
    "session",
    "source",
    "user",
    "기억",
    "내용",
    "사용자",
    "세션",
    "작업",
    "프로젝트",
}
_EP_PROJECT_RE = re.compile(r"(?mi)^project:\s*(.+?)\s*$")
_TEST_NODE_TOKEN_RE = re.compile(r"(?i)(?<![a-z0-9])(test|verify|placeholder)(?![a-z0-9])|테스트")


def _now_iso() -> str:
    return datetime.now(tz=timezone.utc).isoformat()


def _content_hash(title: str, summary: str, tags: str) -> str:
    """노드 콘텐츠의 sha256 앞 16자 — 변경 감지용"""
    raw = f"{title}|{summary}|{tags}"
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


def _normalized_keywords(raw: Any) -> set[str]:
    values: list[Any]
    if isinstance(raw, (list, tuple, set)):
        values = list(raw)
    elif isinstance(raw, dict):
        values = list(raw.values())
    else:
        text = str(raw or "")
        try:
            parsed = json.loads(text)
        except (TypeError, ValueError, json.JSONDecodeError):
            values = [text]
        else:
            if isinstance(parsed, dict):
                values = list(parsed.values())
            elif isinstance(parsed, list):
                values = parsed
            else:
                values = [parsed]

    words: set[str] = set()
    pending = list(values)
    while pending:
        value = pending.pop()
        if isinstance(value, dict):
            pending.extend(value.values())
        elif isinstance(value, (list, tuple, set)):
            pending.extend(value)
        elif value is not None:
            for token in re.findall(r"[^\W_]+", str(value), flags=re.UNICODE):
                normalized = token.casefold()
                if len(normalized) < 2 or normalized.isdigit():
                    continue
                if normalized not in EP_TO_KG_KEYWORD_STOPWORDS:
                    words.add(normalized)
    return words


def _episode_project(content: str) -> str:
    match = _EP_PROJECT_RE.search(content or "")
    return match.group(1).strip() if match else ""


def _kg_project_group(path: str) -> str:
    parts = [part for part in re.split(r"[\\/]+", path or "") if part]
    if len(parts) >= 2 and parts[0].casefold() == "projects":
        return parts[1].casefold()
    return ""


def _is_test_kg_node(node_id: str, title: str) -> bool:
    return bool(_TEST_NODE_TOKEN_RE.search(f"{node_id} {title}"))


def _kg_candidate_allowed(
    node_id: str,
    node_type: str,
    node_path: str,
    node_title: str,
    anchor_id: str,
    anchor_group: str,
) -> bool:
    if _is_test_kg_node(node_id, node_title):
        return False
    if not anchor_group:
        return True
    candidate_group = _kg_project_group(node_path)
    if node_type == "project" and node_id != anchor_id:
        return False
    return not candidate_group or candidate_group == anchor_group


def _load_kg_scope_cache() -> dict[str, tuple[str, str, str]]:
    from core.storage.db import get_connection

    conn = get_connection()
    try:
        rows = conn.execute("SELECT id, type, path, title FROM kg_nodes").fetchall()
        return {
            str(row[0]): (str(row[1] or ""), str(row[2] or ""), str(row[3] or ""))
            for row in rows
        }
    finally:
        conn.close()


def _resolve_episode_scope(content: str, kg_scope_cache: dict[str, tuple[str, str, str]]) -> tuple[str, str]:
    project_key = _episode_project(content)
    if not project_key:
        return "", ""
    anchor_id = resolve_kg_node_id(project_key) or ""
    anchor_meta = kg_scope_cache.get(anchor_id)
    return anchor_id, _kg_project_group(anchor_meta[1]) if anchor_meta else "__unresolved__"


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
        self._write_lock = CrossLoopAsyncLock()
        self._sync_lock = CrossLoopAsyncLock()  # sync_from_kg 동시 실행 방지
        self._encoder_lock = CrossLoopAsyncLock()  # 임베딩 모델 lazy-load 이중 실행 방지
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
        for property_name, migration in MIGRATION_DDL:
            try:
                conn.execute(migration + ";")
                logger.info("%s 컬럼 추가 (마이그레이션)", property_name)
            except Exception as exc:
                message = str(exc).casefold()
                if not any(marker in message for marker in ("already exists", "already has", "duplicate")):
                    raise

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

    async def sync_from_kg(self, cancel_event: threading.Event | None = None) -> dict:
        """
        SQLite kg_nodes / kg_edges 를 KuzuDB 에 동기화한다.
        content_hash 기반으로 변경된 노드만 임베딩을 재계산한다.
        _sync_lock 으로 동시 실행을 방지한다 (edge 중복 증폭 방지).
        """
        if not self._enabled:
            return {"status": "disabled"}

        if not self._sync_lock.try_acquire():
            logger.debug("sync_from_kg: 이미 실행 중 — 스킵")
            return {"status": "skipped"}

        try:
            from core.storage.db import get_connection

            conn = get_connection()
            try:
                nodes = conn.execute("SELECT id, title, type, tags, summary FROM kg_nodes").fetchall()
                node_synced = 0
                reembedded = 0
                for row in nodes:
                    if cancel_event is not None and cancel_event.is_set():
                        return {
                            "status": "cancelled",
                            "nodes": node_synced,
                            "reembedded": reembedded,
                            "edges": 0,
                        }
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

                if cancel_event is not None and cancel_event.is_set():
                    return {
                        "status": "cancelled",
                        "nodes": node_synced,
                        "reembedded": reembedded,
                        "edges": 0,
                    }

                # 엣지는 clear 이후 중단하면 부분 그래프가 되므로 한 번에 끝까지 재생성한다.
                await self.clear_edges()
                edges = conn.execute("SELECT from_id, to_id, rel_type, weight FROM kg_edges").fetchall()
                edge_synced = 0
                for row in edges:
                    await self.create_edge(row[0], row[1], row[2], float(row[3]))
                    edge_synced += 1
            finally:
                conn.close()

            logger.info("SemanticGraph 동기화: nodes=%d (재임베딩=%d), edges=%d", node_synced, reembedded, edge_synced)
            return {"status": "ok", "nodes": node_synced, "reembedded": reembedded, "edges": edge_synced}
        finally:
            self._sync_lock.release()

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

    async def _semantic_search_locked(
        self,
        query_vec: list,
        top_k: int,
        threshold: float,
        *,
        raise_on_error: bool = False,
    ) -> list[dict]:
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
            return rows[:top_k] if top_k > 0 else rows
        except Exception as exc:
            logger.warning("semantic_search 실패: %s", exc)
            if raise_on_error:
                raise
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

    async def graph_retrieve_from_episodes(
        self,
        episode_hits: list[dict],
        *,
        max_hops: int | None = None,
        top_k: int | None = None,
        hop_decay: float | None = None,
        min_score: float | None = None,
    ) -> list[dict]:
        """Episode 검색 결과에서 EP_TO_KG와 KG_EDGE를 따라 관련 KG 노드를 찾는다.

        KG traversal은 최대 2홉으로 강제한다. 각 결과 점수는 Episode 검색 점수,
        EP_TO_KG 신뢰도, KG_EDGE 가중치, 홉 감쇠를 곱해 계산한다.
        """
        if not self._enabled or not episode_hits:
            return []
        if not bool(get_cfg_value("memory.graph_retrieval.enabled", True)):
            return []

        configured_hops = int(get_cfg_value("memory.graph_retrieval.max_hops", 2))
        effective_hops = configured_hops if max_hops is None else int(max_hops)
        effective_hops = max(0, min(effective_hops, 2))
        effective_top_k = (
            int(get_cfg_value("memory.graph_retrieval.top_k", 6))
            if top_k is None
            else int(top_k)
        )
        effective_decay = (
            float(get_cfg_value("memory.graph_retrieval.hop_decay", 0.75))
            if hop_decay is None
            else float(hop_decay)
        )
        effective_decay = max(0.0, min(effective_decay, 1.0))
        effective_min_score = (
            float(get_cfg_value("memory.graph_retrieval.min_score", 0.12))
            if min_score is None
            else float(min_score)
        )

        def _confidence(value: Any, fallback: float = 0.0) -> float:
            try:
                parsed = float(value)
            except (TypeError, ValueError):
                parsed = fallback
            return max(0.0, min(parsed, 1.0))

        results: dict[str, dict] = {}
        try:
            async with self._write_lock:
                for episode_hit in episode_hits:
                    episode_id = str(episode_hit.get("id", "") or "")
                    episode_score = _confidence(episode_hit.get("score"))
                    if not episode_id or episode_score <= 0.0:
                        continue

                    anchor_rows = await self.async_conn.execute(
                        "MATCH (e:EpisodeNode {id: $eid})-[r:EP_TO_KG]->(k:KGNode) "
                        "RETURN k.id, k.title, k.type, k.summary, "
                        "r.rel_type, r.score, r.weight",
                        {"eid": episode_id},
                    )
                    frontier: list[dict] = []
                    while anchor_rows.has_next():
                        row = anchor_rows.get_next()
                        edge_score = _confidence(row[5], _confidence(row[6], 1.0))
                        if edge_score <= 0.0:
                            edge_score = _confidence(row[6], 1.0)
                        score = episode_score * edge_score
                        if score < effective_min_score:
                            continue
                        node_id = str(row[0])
                        path = [
                            {"kind": "episode", "id": episode_id},
                            {
                                "kind": "edge",
                                "type": "EP_TO_KG",
                                "rel_type": str(row[4] or ""),
                                "weight": round(edge_score, 4),
                            },
                            {
                                "kind": "kg",
                                "id": node_id,
                                "title": str(row[1] or node_id),
                            },
                        ]
                        candidate = {
                            "id": node_id,
                            "title": str(row[1] or node_id),
                            "type": str(row[2] or ""),
                            "summary": str(row[3] or ""),
                            "score": round(score, 4),
                            "hop": 0,
                            "episode_id": episode_id,
                            "episode_score": round(episode_score, 4),
                            "path": path,
                        }
                        if score > float(results.get(node_id, {}).get("score", -1.0)):
                            results[node_id] = candidate
                        frontier.append(
                            {
                                **candidate,
                                "score": score,
                                "path_ids": {node_id},
                            }
                        )

                    best_expansion: dict[tuple[str, int], float] = {}
                    for hop in range(1, effective_hops + 1):
                        next_frontier: list[dict] = []
                        for current in frontier:
                            node_id = current["id"]
                            neighbor_specs = (
                                (
                                    "MATCH (n:KGNode {id: $id})-[r:KG_EDGE]->(m:KGNode) "
                                    "RETURN m.id, m.title, m.type, m.summary, "
                                    "r.rel_type, r.weight",
                                    "out",
                                ),
                                (
                                    "MATCH (m:KGNode)-[r:KG_EDGE]->(n:KGNode {id: $id}) "
                                    "RETURN m.id, m.title, m.type, m.summary, "
                                    "r.rel_type, r.weight",
                                    "in",
                                ),
                            )
                            for query, direction in neighbor_specs:
                                neighbor_rows = await self.async_conn.execute(query, {"id": node_id})
                                while neighbor_rows.has_next():
                                    row = neighbor_rows.get_next()
                                    neighbor_id = str(row[0])
                                    if neighbor_id in current["path_ids"]:
                                        continue
                                    edge_weight = _confidence(row[5], 1.0)
                                    score = float(current["score"]) * edge_weight * effective_decay
                                    if score < effective_min_score:
                                        continue
                                    expansion_key = (neighbor_id, hop)
                                    if score <= best_expansion.get(expansion_key, -1.0):
                                        continue
                                    best_expansion[expansion_key] = score
                                    path = [
                                        *current["path"],
                                        {
                                            "kind": "edge",
                                            "type": "KG_EDGE",
                                            "rel_type": str(row[4] or ""),
                                            "direction": direction,
                                            "weight": round(edge_weight, 4),
                                        },
                                        {
                                            "kind": "kg",
                                            "id": neighbor_id,
                                            "title": str(row[1] or neighbor_id),
                                        },
                                    ]
                                    candidate = {
                                        "id": neighbor_id,
                                        "title": str(row[1] or neighbor_id),
                                        "type": str(row[2] or ""),
                                        "summary": str(row[3] or ""),
                                        "score": round(score, 4),
                                        "hop": hop,
                                        "episode_id": current["episode_id"],
                                        "episode_score": current["episode_score"],
                                        "path": path,
                                    }
                                    if score > float(results.get(neighbor_id, {}).get("score", -1.0)):
                                        results[neighbor_id] = candidate
                                    next_frontier.append(
                                        {
                                            **candidate,
                                            "score": score,
                                            "path_ids": {*current["path_ids"], neighbor_id},
                                        }
                                    )
                        frontier = next_frontier
                        if not frontier:
                            break
        except Exception as exc:
            logger.warning("graph_retrieve_from_episodes 실패: %s", exc)
            return []

        ranked = sorted(
            results.values(),
            key=lambda item: (float(item["score"]), -int(item["hop"]), item["id"]),
            reverse=True,
        )
        return ranked[:effective_top_k] if effective_top_k > 0 else ranked

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
            # EP_TO_KG 자동 연결 (임베딩이 없어도 키워드 연결은 수행)
            await self._link_episode_to_kg_locked(
                episode_id=episode_id,
                episode_vec=emb or None,
                episode_keywords=keywords,
                episode_content=content,
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
        sem_threshold: float | None = None,
        kw_threshold: int = 2,
        kg_keyword_cache: list[tuple] | None = None,
        kg_scope_cache: dict[str, tuple[str, str, str]] | None = None,
    ) -> int:
        """EpisodeNode를 관련 KGNode들에 EP_TO_KG 릴레이션으로 연결.

        연결 기준:
        1. 시맨틱 유사도 >= sem_threshold 인 KGNode (rel_type='semantic')
        2. keywords 겹침 >= kw_threshold 단어 (rel_type='keyword')

        Returns: 생성된 릴레이션 수
        """
        async with self._write_lock:
            return await self._link_episode_to_kg_locked(
                episode_id,
                episode_vec,
                episode_keywords,
                "",
                top_k,
                sem_threshold,
                kw_threshold,
                kg_keyword_cache,
                kg_scope_cache,
            )

    async def _link_episode_to_kg_locked(
        self,
        episode_id: str,
        episode_vec: list | None = None,
        episode_keywords: str = "",
        episode_content: str = "",
        top_k: int = 3,
        sem_threshold: float | None = None,
        kw_threshold: int = 2,
        kg_keyword_cache: list[tuple] | None = None,
        kg_scope_cache: dict[str, tuple[str, str, str]] | None = None,
    ) -> int:
        """호출자가 이미 self._write_lock을 쥔 상태에서만 호출
        (upsert_episode/sync_all_ep_to_kg 내부)."""
        if not self._enabled:
            return 0

        linked_at = _now_iso()
        if sem_threshold is None:
            sem_threshold = float(get_cfg_value("memory.ep_to_kg.semantic_threshold", 0.55))
        if not episode_content:
            try:
                res = await self.async_conn.execute(
                    "MATCH (e:EpisodeNode {id: $eid}) RETURN e.content",
                    {"eid": episode_id},
                )
                if res.has_next():
                    episode_content = res.get_next()[0] or ""
            except Exception:
                episode_content = ""
        episode_project = _episode_project(episode_content)
        if kg_scope_cache is None and episode_project:
            try:
                kg_scope_cache = _load_kg_scope_cache()
            except Exception:
                kg_scope_cache = {}
        elif kg_scope_cache is None:
            kg_scope_cache = {}
        anchor_id, anchor_group = _resolve_episode_scope(episode_content, kg_scope_cache)

        # 1. 시맨틱 연결
        semantic_candidates: list[dict] = []
        if episode_vec is not None:
            try:
                sem_hits = await self._semantic_search_locked(
                    episode_vec,
                    0,
                    sem_threshold,
                    raise_on_error=True,
                )
            except Exception:
                return 0
            for hit in sem_hits:
                node_type, node_path, node_title = kg_scope_cache.get(
                    str(hit["id"]),
                    (str(hit.get("type", "") or ""), "", str(hit.get("title", "") or "")),
                )
                if not _kg_candidate_allowed(
                    str(hit["id"]),
                    node_type,
                    node_path,
                    node_title,
                    anchor_id,
                    anchor_group,
                ):
                    continue
                semantic_candidates.append(hit)
                if len(semantic_candidates) >= top_k:
                    break

        # 2. 키워드 연결
        keyword_candidates: list[tuple[float, float, str, list[str]]] = []
        try:
            ep_words = _normalized_keywords(episode_keywords)

            if ep_words:
                rows_to_scan = kg_keyword_cache
                if rows_to_scan is None:
                    res = await self.async_conn.execute(
                        "MATCH (k:KGNode) WHERE k.tags <> '' OR k.title <> '' "
                        "RETURN k.id, k.tags, k.title, k.type"
                    )
                    rows_to_scan = []
                    while res.has_next():
                        rows_to_scan.append(res.get_next())

                for row in rows_to_scan:
                    kg_id, tags_raw, title = str(row[0]), row[1] or "", row[2] or ""
                    node_type, node_path, cached_title = kg_scope_cache.get(
                        kg_id,
                        (str(row[3] or "") if len(row) > 3 else "", "", str(title)),
                    )
                    if not _kg_candidate_allowed(
                        kg_id,
                        node_type,
                        node_path,
                        cached_title or str(title),
                        anchor_id,
                        anchor_group,
                    ):
                        continue
                    kg_words = _normalized_keywords(tags_raw) | _normalized_keywords(title)
                    intersection = ep_words & kg_words
                    if len(intersection) >= kw_threshold:
                        matched = sorted(intersection)
                        overlap = float(len(matched))
                        score = overlap / max(1, len(kg_words))
                        keyword_candidates.append((score, overlap, kg_id, matched))

                keyword_candidates.sort(key=lambda item: (-item[0], -item[1], item[2]))
                keyword_candidates = keyword_candidates[:top_k]
        except Exception as exc:
            logger.warning("EP_TO_KG keyword candidate 계산 실패 (id=%s): %s", episode_id, exc)
            return 0

        try:
            created = await asyncio.to_thread(
                self._replace_episode_links_transaction,
                episode_id,
                semantic_candidates,
                keyword_candidates,
                linked_at,
            )
        except Exception as exc:
            logger.warning("EP_TO_KG atomic replace 실패 (id=%s): %s", episode_id, exc)
            return 0

        if created:
            logger.debug("EP_TO_KG: episode=%s, %d 릴레이션 생성", episode_id, created)
        return created

    def _replace_episode_links_transaction(
        self,
        episode_id: str,
        semantic_candidates: list[dict],
        keyword_candidates: list[tuple[float, float, str, list[str]]],
        linked_at: str,
    ) -> int:
        conn = self.async_conn.acquire_connection()
        created = 0
        try:
            conn.execute("BEGIN TRANSACTION")
            conn.execute(
                "MATCH (e:EpisodeNode {id: $eid})-[r:EP_TO_KG]->() "
                "WHERE r.rel_type IN ['semantic', 'keyword'] DELETE r",
                {"eid": episode_id},
            )
            for hit in semantic_candidates:
                conn.execute(
                    "MERGE (e:EpisodeNode {id: $eid}) "
                    "MERGE (k:KGNode {id: $kid}) "
                    "MERGE (e)-[r:EP_TO_KG {rel_type: 'semantic'}]->(k) "
                    "SET r.weight=$score, r.keywords='', r.score=$score, "
                    "r.method='semantic', r.model=$model, r.version=$version, "
                    "r.created_at=$created_at",
                    {
                        "eid": episode_id,
                        "kid": hit["id"],
                        "score": float(hit["score"]),
                        "model": self._embedding_model_name,
                        "version": EP_TO_KG_LINK_VERSION,
                        "created_at": linked_at,
                    },
                )
                created += 1
            for score, overlap, kg_id, matched in keyword_candidates:
                conn.execute(
                    "MERGE (e:EpisodeNode {id: $eid}) "
                    "MERGE (k:KGNode {id: $kid}) "
                    "MERGE (e)-[r:EP_TO_KG {rel_type: 'keyword'}]->(k) "
                    "SET r.weight=$weight, r.keywords=$matched, r.score=$score, "
                    "r.method='keyword', r.model='', r.version=$version, "
                    "r.created_at=$created_at",
                    {
                        "eid": episode_id,
                        "kid": kg_id,
                        "weight": overlap,
                        "score": score,
                        "matched": ", ".join(matched),
                        "version": EP_TO_KG_LINK_VERSION,
                        "created_at": linked_at,
                    },
                )
                created += 1
            conn.execute("COMMIT")
            return created
        except Exception:
            try:
                conn.execute("ROLLBACK")
            except Exception:
                logger.exception("Failed to roll back EP_TO_KG replacement")
            raise
        finally:
            self.async_conn.release_connection(conn)

    async def sync_all_ep_to_kg(self, sem_threshold: float | None = None, top_k: int = 3) -> dict:
        """기존 EpisodeNode 전체에 link_episode_to_kg 소급 적용.
        MCP 서버 중단 후 실행해야 함 (KuzuDB 단일 writer 제약).
        Returns: {"processed": N, "linked": M}
        """
        async with self._write_lock:
            return await self._sync_all_ep_to_kg_locked(sem_threshold, top_k)

    async def _sync_all_ep_to_kg_locked(self, sem_threshold: float | None = None, top_k: int = 3) -> dict:
        if not self._enabled:
            return {"processed": 0, "linked": 0, "error": "KuzuDB disabled"}

        # KGNode keyword/title 캐시 1회 빌드 → link_episode_to_kg 호출마다 전체 스캔 방지
        kg_keyword_cache: list[tuple] | None = None
        try:
            res = await self.async_conn.execute(
                "MATCH (k:KGNode) WHERE k.tags <> '' OR k.title <> '' "
                "RETURN k.id, k.tags, k.title, k.type"
            )
            kg_keyword_cache = []
            while res.has_next():
                kg_keyword_cache.append(res.get_next())
        except Exception:
            kg_keyword_cache = None

        episodes: list[tuple[str, list | None, str, str]] = []
        try:
            res = await self.async_conn.execute(
                "MATCH (e:EpisodeNode) RETURN e.id, e.embedding, e.keywords, e.content"
            )
            while res.has_next():
                ep_id, embedding_raw, keywords, content = res.get_next()
                ep_vec = None
                if embedding_raw:
                    try:
                        ep_vec = json.loads(embedding_raw)
                    except (TypeError, ValueError, json.JSONDecodeError):
                        logger.warning("EpisodeNode embedding 파싱 실패: %s", ep_id)
                episodes.append((str(ep_id), ep_vec, keywords or "", content or ""))
        except Exception as exc:
            raise RuntimeError("EpisodeNode inventory load failed") from exc

        if any(_episode_project(content) for _, _, _, content in episodes):
            try:
                kg_scope_cache = _load_kg_scope_cache()
            except Exception:
                kg_scope_cache = {}
        else:
            kg_scope_cache = {}

        processed = 0
        linked = 0
        for ep_id, ep_vec, keywords, content in episodes:
            n = await self._link_episode_to_kg_locked(
                ep_id,
                episode_vec=ep_vec,
                episode_keywords=keywords,
                episode_content=content,
                top_k=top_k,
                sem_threshold=sem_threshold,
                kg_keyword_cache=kg_keyword_cache,
                kg_scope_cache=kg_scope_cache,
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
        contents: list[str] = []
        dates: list[str] = []
        vecs: list[Any] = []

        try:
            res = await self.async_conn.execute(
                "MATCH (e:EpisodeNode) WHERE e.embedding <> '' "
                "RETURN e.id, e.content, e.created_at, e.embedding"
            )
            while res.has_next():
                row = res.get_next()
                try:
                    vec = np.array(json.loads(row[3]), dtype=np.float32)
                    ids.append(row[0])
                    contents.append(row[1] or "")
                    dates.append(row[2] or "")
                    vecs.append(vec)
                except Exception:
                    pass
        except Exception as exc:
            logger.debug("_rebuild_episode_cache 실패: %s", exc)

        self._episode_cache_matrix = np.stack(vecs) if vecs else None
        self._episode_cache_ids = ids
        self._episode_cache_contents = contents
        self._episode_cache_dates = dates
        self._episode_cache_dirty = False
        logger.debug("_rebuild_episode_cache: %d 에피소드 로드", len(ids))

    async def reconcile_episodes(
        self,
        canonical_memory_ids: Iterable[str | int] | Callable[[], Iterable[str | int]],
        apply: bool = False,
    ) -> EpisodeReconciliationReport:
        """SQLite memories ID 집합을 기준으로 EpisodeNode 무결성을 점검한다."""
        if not self._enabled:
            raise RuntimeError("SemanticGraph is disabled")
        if apply and self._read_only:
            raise RuntimeError("Cannot apply episode reconciliation to a read-only SemanticGraph")

        async with self._write_lock:
            source_ids = canonical_memory_ids() if callable(canonical_memory_ids) else canonical_memory_ids
            canonical_ids = {str(memory_id) for memory_id in source_ids}
            try:
                episode_ids = await self._episode_ids_locked()
                linked_ids = await self._linked_episode_ids_locked()
            except Exception as exc:
                raise RuntimeError("Failed to audit EpisodeNode integrity") from exc

            stale_ids = sorted(episode_ids - canonical_ids, key=_memory_id_sort_key)
            missing_ids = sorted(canonical_ids - episode_ids, key=_memory_id_sort_key)
            unlinked_ids = sorted(episode_ids - linked_ids, key=_memory_id_sort_key)
            episode_count_before = len(episode_ids)
            episode_ids_after = episode_ids

            if apply and stale_ids:
                try:
                    await asyncio.to_thread(
                        self._delete_stale_episodes_transaction,
                        stale_ids,
                    )
                    episode_ids_after = await self._episode_ids_locked()
                except Exception as exc:
                    self._episode_cache_dirty = True
                    raise RuntimeError("Failed to delete stale EpisodeNodes") from exc
                self._episode_cache_dirty = True

            return {
                "canonical_count": len(canonical_ids),
                "episode_count_before": episode_count_before,
                "stale_ids": stale_ids,
                "missing_ids": missing_ids,
                "unlinked_episode_ids": unlinked_ids,
                "applied": apply,
                "deleted_count": len(episode_ids - episode_ids_after),
                "episode_count_after": len(episode_ids_after),
            }

    def _delete_stale_episodes_transaction(self, stale_ids: list[str]) -> None:
        conn = self.async_conn.acquire_connection()
        try:
            conn.execute("BEGIN TRANSACTION")
            for episode_id in stale_ids:
                conn.execute(
                    "MATCH (e:EpisodeNode {id: $id})-[r:EP_TO_KG]->() DELETE r",
                    {"id": episode_id},
                )
                conn.execute(
                    "MATCH (e:EpisodeNode {id: $id}) DELETE e",
                    {"id": episode_id},
                )
            conn.execute("COMMIT")
        except Exception:
            try:
                conn.execute("ROLLBACK")
            except Exception:
                logger.exception("Failed to roll back stale EpisodeNode deletion")
            raise
        finally:
            self.async_conn.release_connection(conn)

    async def _episode_ids_locked(self) -> set[str]:
        res = await self.async_conn.execute("MATCH (e:EpisodeNode) RETURN e.id")
        ids: set[str] = set()
        while res.has_next():
            ids.add(str(res.get_next()[0]))
        return ids

    async def _linked_episode_ids_locked(self) -> set[str]:
        res = await self.async_conn.execute(
            "MATCH (e:EpisodeNode)-[:EP_TO_KG]->() RETURN DISTINCT e.id"
        )
        ids: set[str] = set()
        while res.has_next():
            ids.add(str(res.get_next()[0]))
        return ids

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
                            "content": self._episode_cache_contents[i],
                            "score": round(score, 4),
                            "created_at": self._episode_cache_dates[i],
                        }
                    )
                rows.sort(key=lambda x: x["score"], reverse=True)
                if top_k > 0:
                    rows = rows[:top_k]

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
