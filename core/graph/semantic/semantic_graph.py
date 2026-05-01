"""
Semantic Knowledge Graph — KuzuDB + sentence-transformers

SQLite kg_nodes/kg_edges 를 KuzuDB 에 미러링하여 시맨틱 검색을 지원한다.
connectomeLLM_AGA 의 LongTermMemory 패턴을 Knowledge Graph 에 맞게 포팅.

DB 위치: {db_root}/semantic_graph  (KuzuDB embedded, 파일 기반)
임베딩 모델: paraphrase-multilingual-MiniLM-L12-v2  (한국어/영어 다국어 지원)
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from core.config.runtime_config import get_db_root_dir

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
        self._write_lock = threading.RLock()
        self._sync_lock = threading.Lock()  # sync_from_kg 동시 실행 방지
        self._read_only = read_only
        self._enabled = False
        self._embedding_model_name = embedding_model
        self._encoder: Any = None
        self.db: Any = None
        self.conn: Any = None

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
            self.conn = kuzu.Connection(self.db)
            if not read_only:
                self._init_schema()
            self._enabled = True
            mode = "read-only" if read_only else "read-write"
            logger.info("SemanticGraph initialised at %s (%s)", db_path, mode)
        except ImportError:
            logger.warning("kuzu 미설치 — SemanticGraph 비활성화. `pip install kuzu`")
        except Exception as exc:
            logger.warning("SemanticGraph 초기화 실패 (%s) — 비활성화", exc)

    # ── 스키마 초기화 ─────────────────────────────────────────────────────────

    def _init_schema(self) -> None:
        for stmt in SCHEMA_DDL.strip().split(";"):
            stmt = stmt.strip()
            if stmt:
                try:
                    self.conn.execute(stmt + ";")
                except Exception:
                    logger.debug("_init_schema: DDL 스킵 (already exists): %s", stmt[:60])
        # 기존 DB에 content_hash 컬럼이 없으면 마이그레이션
        try:
            self.conn.execute(MIGRATION_DDL + ";")
            logger.info("KGNode.content_hash 컬럼 추가 (마이그레이션)")
        except Exception:
            pass  # 이미 존재하면 정상

    # ── 임베딩 캐시 헬퍼 ──────────────────────────────────────────────────────

    def _invalidate_cache(self) -> None:
        self._cache_dirty = True

    def _rebuild_cache(self) -> None:
        """모든 KGNode 임베딩을 numpy matrix로 로드 (다음 검색 때 한 번만 실행)."""
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
            res = self.conn.execute("MATCH (n:KGNode) WHERE n.embedding <> '' " "RETURN n.id, n.title, n.type, n.summary, n.embedding")
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

    def _ensure_cache(self) -> None:
        with self._write_lock:
            if self._cache_dirty:
                self._rebuild_cache()

    # ── 임베딩 헬퍼 ───────────────────────────────────────────────────────────

    def _get_encoder(self) -> Any:
        if self._encoder is None:
            try:
                from sentence_transformers import SentenceTransformer
                import transformers as _tr

                prev = _tr.logging.get_verbosity()
                _tr.logging.set_verbosity_error()
                try:
                    try:
                        self._encoder = SentenceTransformer(self._embedding_model_name, local_files_only=True, device="cpu")
                        logger.info("임베딩 모델 로컬 캐시에서 로드: %s", self._embedding_model_name)
                    except Exception:
                        logger.info("Hub에서 다운로드: %s", self._embedding_model_name)
                        self._encoder = SentenceTransformer(self._embedding_model_name, device="cpu")
                finally:
                    _tr.logging.set_verbosity(prev)
            except Exception as exc:
                logger.warning("임베딩 모델 로드 실패: %s", exc)
                self._encoder = False  # 재시도 방지 sentinel
        return self._encoder if self._encoder is not False else None

    def compute_embedding(self, text: str) -> list[float]:
        """텍스트를 384-dim 정규화 벡터로 변환. 실패 시 빈 리스트 반환."""
        enc = self._get_encoder()
        if enc is None:
            return []
        try:
            vec = enc.encode(text.strip()[:512], normalize_embeddings=True, show_progress_bar=False)
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

    def upsert_node(
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
        with self._write_lock:
            if not self._enabled:
                return False
            now = _now_iso()
            tags_str = json.dumps(tags, ensure_ascii=False) if isinstance(tags, list) else tags
            new_hash = _content_hash(title, summary, tags_str)

            # 기존 노드의 hash + embedding 조회
            old_hash = ""
            old_emb = ""
            try:
                res = self.conn.execute(
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
                emb = self.compute_embedding(self._node_text(title, summary, tags))
                emb_str = json.dumps(emb) if emb else ""
                recomputed = True
            else:
                emb_str = old_emb

            try:
                self.conn.execute(
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

    def clear_edges(self) -> None:
        with self._write_lock:
            if not self._enabled:
                return
            try:
                self.conn.execute("MATCH ()-[e:KG_EDGE]->() DELETE e")
            except Exception as exc:
                logger.debug("clear_edges 실패: %s", exc)

    def create_edge(self, from_id: str, to_id: str, rel_type: str = "links", weight: float = 1.0) -> None:
        with self._write_lock:
            if not self._enabled:
                return
            try:
                self.conn.execute(
                    "MATCH (a:KGNode {id: $fid}), (b:KGNode {id: $tid}) "
                    "MERGE (a)-[e:KG_EDGE {rel_type: $rel}]->(b) "
                    "ON CREATE SET e.weight=$w "
                    "ON MATCH SET e.weight=$w",
                    {"fid": from_id, "tid": to_id, "rel": rel_type, "w": weight},
                )
            except Exception as exc:
                logger.debug("create_edge 스킵 (%s→%s): %s", from_id, to_id, exc)

    # ── SQLite KG → KuzuDB 동기화 ────────────────────────────────────────────

    def sync_from_kg(self) -> dict:
        """
        SQLite kg_nodes / kg_edges 를 KuzuDB 에 동기화한다.
        content_hash 기반으로 변경된 노드만 임베딩을 재계산한다.
        _sync_lock 으로 동시 실행을 방지한다 (edge 중복 증폭 방지).
        """
        if not self._enabled:
            return {"status": "disabled"}

        if not self._sync_lock.acquire(blocking=False):
            logger.debug("sync_from_kg: 이미 실행 중 — 스킵")
            return {"status": "skipped"}

        try:
            from core.storage.db import get_connection

            conn = get_connection()

            nodes = conn.execute("SELECT id, title, type, tags, summary FROM kg_nodes").fetchall()
            node_synced = 0
            reembedded = 0
            for row in nodes:
                nid, title, ntype, tags_json, summary = row
                did_reembed = self.upsert_node(
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
            self.clear_edges()
            edges = conn.execute("SELECT from_id, to_id, rel_type, weight FROM kg_edges").fetchall()
            edge_synced = 0
            for row in edges:
                self.create_edge(row[0], row[1], row[2], float(row[3]))
                edge_synced += 1

            conn.close()
            logger.info("SemanticGraph 동기화: nodes=%d (재임베딩=%d), edges=%d", node_synced, reembedded, edge_synced)
            return {"status": "ok", "nodes": node_synced, "reembedded": reembedded, "edges": edge_synced}
        finally:
            self._sync_lock.release()

    # ── 시맨틱 검색 ───────────────────────────────────────────────────────────

    def semantic_search(
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
            query_vec = self.compute_embedding(query)
        if not query_vec:
            return []
        try:
            import numpy as np

            self._ensure_cache()
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

    def semantic_neighbors(self, node_id: str, top_k: int = 5) -> list[dict]:
        """특정 노드와 의미적으로 가장 유사한 노드를 반환 (캐시 기반)."""
        if not self._enabled:
            return []
        try:
            self._ensure_cache()
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

    def upsert_episode(
        self,
        episode_id: str,
        content: str,
        keywords: str = "",
        session_id: str = "",
        created_at: str = "",
    ) -> bool:
        """에피소드를 KuzuDB EpisodeNode에 upsert. 임베딩은 content 기반으로 계산.
        Returns True on success, False on failure."""
        with self._write_lock:
            if not self._enabled:
                return False
            if not created_at:
                created_at = _now_iso()
            emb = self.compute_embedding(content.strip()[:512])
            emb_str = json.dumps(emb) if emb else ""
            try:
                self.conn.execute(
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
                    self.link_episode_to_kg(
                        episode_id=episode_id,
                        episode_vec=emb,
                        episode_keywords=keywords,
                    )
                return True
            except Exception as exc:
                logger.debug("upsert_episode 실패 (id=%s): %s", episode_id, exc)
                return False

    def link_episode_to_kg(
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
        with self._write_lock:
            if not self._enabled:
                return 0

            created = 0

            # 기존 EP_TO_KG 삭제 (rel_type='semantic' 또는 'keyword' 만 대상, 다른 타입 보존)
            try:
                self.conn.execute(
                    "MATCH (e:EpisodeNode {id: $eid})-[r:EP_TO_KG]->() " "WHERE r.rel_type IN ['semantic', 'keyword'] DELETE r",
                    {"eid": episode_id},
                )
            except Exception as exc:
                logger.debug("EP_TO_KG 기존 edge 삭제 실패 (id=%s): %s", episode_id, exc)

            # 1. 시맨틱 연결
            if episode_vec is not None:
                sem_hits = self.semantic_search(query="", top_k=top_k, threshold=sem_threshold, query_vec=episode_vec)
                for hit in sem_hits:
                    try:
                        self.conn.execute(
                            "MERGE (e:EpisodeNode {id: $eid}) "
                            "MERGE (k:KGNode {id: $kid}) "
                            "MERGE (e)-[r:EP_TO_KG {rel_type: 'semantic'}]->(k)",
                            {"eid": episode_id, "kid": hit["id"]},
                        )
                        created += 1
                    except Exception as exc:
                        logger.debug("EP_TO_KG semantic link 실패 (%s→%s): %s", episode_id, hit["id"], exc)

            # 2. 키워드 연결 (정규화된 테이블 활용)
            try:
                # SQLite에서 현재 에피소드의 정규화된 키워드 목록 가져오기
                from core.db import get_connection

                sqlite_conn = get_connection()
                ep_keywords_rows = sqlite_conn.execute(
                    "SELECT k.name FROM keywords k " "JOIN memory_keywords mk ON k.id = mk.keyword_id " "WHERE mk.memory_id = ?", (episode_id,)
                ).fetchall()
                ep_words = set(row["name"] for row in ep_keywords_rows)
                sqlite_conn.close()

                if ep_words:
                    rows_to_scan = kg_keyword_cache
                    if rows_to_scan is None:
                        res = self.conn.execute("MATCH (k:KGNode) WHERE k.tags <> '' OR k.title <> '' RETURN k.id, k.tags, k.title")
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
                            self.conn.execute(
                                "MERGE (e:EpisodeNode {id: $eid}) "
                                "MERGE (k:KGNode {id: $kid}) "
                                "MERGE (e)-[r:EP_TO_KG {rel_type: 'keyword'}]->(k) "
                                "SET r.weight = $weight, r.keywords = $matched",
                                {"eid": episode_id, "kid": kg_id, "weight": len(intersection), "matched": ", ".join(list(intersection))},
                            )
                            created += 1
            except Exception as exc:
                logger.debug("EP_TO_KG keyword link (normalized) 실패: %s", exc)

            if created:
                logger.debug("EP_TO_KG: episode=%s, %d 릴레이션 생성", episode_id, created)
            return created

    def sync_all_ep_to_kg(self, sem_threshold: float = 0.40, top_k: int = 3) -> dict:
        """기존 EpisodeNode 전체에 link_episode_to_kg 소급 적용.
        MCP 서버 중단 후 실행해야 함 (KuzuDB 단일 writer 제약).
        Returns: {"processed": N, "linked": M}
        """
        with self._write_lock:
            if not self._enabled:
                return {"processed": 0, "linked": 0, "error": "KuzuDB disabled"}

            if self._episode_cache_dirty:
                self._rebuild_episode_cache()

            # KGNode keyword/title 캐시 1회 빌드 → link_episode_to_kg 호출마다 전체 스캔 방지
            kg_keyword_cache: list[tuple] | None = None
            try:
                res = self.conn.execute("MATCH (k:KGNode) WHERE k.tags <> '' RETURN k.id, k.tags, k.title")
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
                    res = self.conn.execute("MATCH (e:EpisodeNode {id: $id}) RETURN e.keywords", {"id": ep_id})
                    if res.has_next():
                        keywords = res.get_next()[0] or ""
                except Exception:
                    pass
                n = self.link_episode_to_kg(
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

    def _rebuild_episode_cache(self) -> None:
        """EpisodeNode 임베딩 캐시 재빌드."""
        with self._write_lock:
            try:
                import numpy as np
            except ImportError:
                return

            ids: list[str] = []
            dates: list[str] = []
            vecs: list[Any] = []

            try:
                # content 제거: id, created_at, embedding만 조회
                res = self.conn.execute("MATCH (e:EpisodeNode) WHERE e.embedding <> '' " "RETURN e.id, e.created_at, e.embedding")
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

    def episode_semantic_search(
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
            query_vec = self.compute_embedding(query)
        if not query_vec:
            return []
        try:
            import numpy as np
            from datetime import datetime, timedelta, timezone

            if self._episode_cache_dirty:
                self._rebuild_episode_cache()
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

            # top-k ids로 content 배치 조회 (캐시에 content를 상주시키지 않음)
            if rows:
                top_ids = [r["id"] for r in rows]
                content_map: dict[str, str] = {}
                try:
                    for ep_id in top_ids:
                        res2 = self.conn.execute(
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

    def cypher_query(self, cypher: str, params: dict | None = None) -> list[dict]:
        """KuzuDB에 직접 Cypher 쿼리를 실행한다. 결과는 list of dict로 반환."""
        if not self._enabled:
            return []
        try:
            res = self.conn.execute(cypher, params or {})
            results = []
            col_names = res.get_column_names()
            while res.has_next():
                row = res.get_next()
                results.append(dict(zip(col_names, row)))
            return results
        except Exception as exc:
            logger.warning("cypher_query 실패: %s", exc)
            return [{"error": str(exc)}]

    @property
    def enabled(self) -> bool:
        return self._enabled


# ── 싱글턴 ────────────────────────────────────────────────────────────────────

_sg_instance: SemanticGraph | None = None


def get_semantic_graph() -> SemanticGraph:
    global _sg_instance
    if _sg_instance is None:
        _sg_instance = SemanticGraph()
    return _sg_instance

