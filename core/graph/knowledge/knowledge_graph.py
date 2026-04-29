"""
Knowledge Graph — Zettelkasten + GraphDB 레이어
D:\\intel_engram\\docs\\ 의 마크다운 파일을 노드/엣지로 관리.

노드 타입: concept | project | research | reference | fleeting | moc | person | tool
엣지 타입: links | supports | contradicts | part_of | follows | inspired_by | implements | references
"""

from __future__ import annotations

import re
import json
from pathlib import Path
from datetime import datetime
from collections import deque
from typing import Optional

import yaml  # PyYAML (requirements.txt에 있음)

from core.storage.db import get_connection

# ── 타입 정의 ─────────────────────────────────────────────

NODE_TYPES = {"concept", "project", "research", "reference", "fleeting", "moc", "person", "tool"}
EDGE_TYPES = {"links", "supports", "contradicts", "part_of", "follows", "inspired_by", "implements", "references"}

NODE_TYPE_DIRS = {
    "concept":   "concepts",
    "project":   "projects",
    "research":  "research",
    "reference": "references",
    "moc":       "moc",
    "person":    "people",
    "tool":      "tools",
    "fleeting":  "_inbox",
}

NODE_COLORS = {
    "concept":   "#7c6af7",
    "project":   "#4ecdc4",
    "research":  "#f9ca24",
    "reference": "#95a5a6",
    "fleeting":  "#a29bfe",
    "moc":       "#f0932b",
    "person":    "#fd79a8",
    "tool":      "#00b894",
}


# ── 스키마 초기화 ─────────────────────────────────────────

KG_SCHEMA = """
CREATE TABLE IF NOT EXISTS kg_nodes (
    id          TEXT PRIMARY KEY,
    title       TEXT NOT NULL,
    path        TEXT,
    type        TEXT NOT NULL DEFAULT 'concept'
                CHECK (type IN ('concept','project','research','reference','fleeting','moc','person','tool')),
    tags        TEXT NOT NULL DEFAULT '[]',
    summary     TEXT NOT NULL DEFAULT '',
    vault_path  TEXT NOT NULL DEFAULT '',
    created_at  TEXT DEFAULT (datetime('now','localtime')),
    updated_at  TEXT DEFAULT (datetime('now','localtime'))
);

CREATE TABLE IF NOT EXISTS kg_edges (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    from_id     TEXT NOT NULL REFERENCES kg_nodes(id) ON DELETE CASCADE,
    to_id       TEXT NOT NULL REFERENCES kg_nodes(id) ON DELETE CASCADE,
    rel_type    TEXT NOT NULL DEFAULT 'links'
                CHECK (rel_type IN ('links','supports','contradicts','part_of','follows','inspired_by','implements','references')),
    context     TEXT NOT NULL DEFAULT '',
    weight      REAL NOT NULL DEFAULT 1.0,
    created_at  TEXT DEFAULT (datetime('now','localtime')),
    UNIQUE (from_id, to_id, rel_type)
);

CREATE INDEX IF NOT EXISTS idx_kg_nodes_type ON kg_nodes(type);
CREATE INDEX IF NOT EXISTS idx_kg_nodes_title ON kg_nodes(title);
CREATE INDEX IF NOT EXISTS idx_kg_edges_from ON kg_edges(from_id);
CREATE INDEX IF NOT EXISTS idx_kg_edges_to ON kg_edges(to_id);
"""


def initialize_kg_tables():
    conn = get_connection()
    with conn:
        conn.executescript(KG_SCHEMA)
    conn.close()


# ── 슬러그 생성 ───────────────────────────────────────────

def _slugify(title: str) -> str:
    slug = title.lower()
    slug = re.sub(r"[^\w\s가-힣-]", "", slug)
    slug = re.sub(r"[\s_]+", "-", slug.strip())
    return slug[:80]


# ── 마크다운 파싱 ─────────────────────────────────────────

_FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n", re.DOTALL)
_WIKILINK_RE = re.compile(r"\[\[([^\]|#]+?)(?:\|[^\]]+)?\]\]")
_HASHTAG_RE = re.compile(r"(?<!\w)#([a-zA-Z가-힣][a-zA-Z0-9가-힣_-]*)")
_FIRST_PARA_RE = re.compile(r"^#+\s.*?\n+([\s\S]*?)(?:\n\n|\Z)", re.MULTILINE)


def parse_markdown(text: str, filepath: Path | None = None) -> dict:
    """마크다운에서 frontmatter, wikilinks, tags, summary 추출"""
    fm = {}
    body = text

    m = _FRONTMATTER_RE.match(text)
    if m:
        try:
            fm = yaml.safe_load(m.group(1)) or {}
        except yaml.YAMLError:
            fm = {}
        body = text[m.end():]

    # wikilinks
    wikilinks = list(dict.fromkeys(_WIKILINK_RE.findall(body)))

    # tags: frontmatter 우선, body에서 보완
    tags = list(fm.get("tags", []))
    body_tags = _HASHTAG_RE.findall(body)
    for t in body_tags:
        if t not in tags:
            tags.append(t)

    # summary: frontmatter summary > 첫 비어있지 않은 텍스트 문단
    summary = fm.get("summary", "")
    if not summary:
        mp = _FIRST_PARA_RE.search(body)
        if mp:
            summary = mp.group(1).strip()[:200]
        else:
            # 첫 200자
            clean = re.sub(r"[#*`>\[\]\(\)_]", "", body).strip()
            summary = clean[:200]

    title = fm.get("title", "")
    if not title and filepath:
        title = filepath.stem.replace("-", " ").replace("_", " ").title()

    return {
        "id": fm.get("id", _slugify(title) if title else None),
        "title": title,
        "type": fm.get("type") or fm.get("note_type", "concept"),
        "tags": tags,
        "summary": summary,
        "links": list(fm.get("links", [])) + wikilinks,
        "frontmatter": fm,
    }


def build_frontmatter(title: str, note_type: str, tags: list, links: list, summary: str = "", extra: dict | None = None) -> str:
    """노트 생성 시 frontmatter YAML 빌드"""
    fm: dict = {
        "id": _slugify(title),
        "title": title,
        "note_type": note_type,
        "created": datetime.now().strftime("%Y-%m-%d"),
    }
    if tags:
        fm["tags"] = tags
    if links:
        fm["links"] = links
    if summary:
        fm["summary"] = summary
    if extra:
        fm.update(extra)
    return "---\n" + yaml.dump(fm, allow_unicode=True, default_flow_style=False) + "---\n\n"


# ── KnowledgeGraph 클래스 ────────────────────────────────

class KnowledgeGraph:
    """Zettelkasten 지식 그래프 — SQLite 기반"""

    def __init__(self):
        initialize_kg_tables()

    # ── 노드 ──────────────────────────────────────────────

    def add_node(self, title: str, note_type: str = "concept", tags: list | None = None,
                 summary: str = "", path: str = "", vault_path: str = "",
                 node_id: str | None = None) -> str:
        nid = node_id or _slugify(title)
        conn = get_connection()
        with conn:
            conn.execute(
                """
                INSERT INTO kg_nodes (id, title, path, type, tags, summary, vault_path)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    title=excluded.title, path=excluded.path, type=excluded.type,
                    tags=excluded.tags, summary=excluded.summary, vault_path=excluded.vault_path,
                    updated_at=datetime('now','localtime')
                """,
                (nid, title, path, note_type, json.dumps(tags or [], ensure_ascii=False),
                 summary, vault_path),
            )
        conn.close()
        return nid

    def get_node(self, identifier: str) -> dict | None:
        """id 또는 title로 노드 조회"""
        conn = get_connection()
        row = conn.execute(
            "SELECT * FROM kg_nodes WHERE id=? OR lower(title)=lower(?)",
            (identifier, identifier),
        ).fetchone()
        conn.close()
        if not row:
            return None
        return self._row_to_dict(row)

    def delete_node(self, node_id: str) -> bool:
        conn = get_connection()
        with conn:
            cur = conn.execute("DELETE FROM kg_nodes WHERE id=?", (node_id,))
        conn.close()
        return cur.rowcount > 0

    def list_nodes(self, note_type: str | None = None, tag: str | None = None,
                   limit: int = 50) -> list[dict]:
        conn = get_connection()
        if note_type and tag:
            rows = conn.execute(
                "SELECT * FROM kg_nodes WHERE type=? AND tags LIKE ? ORDER BY updated_at DESC LIMIT ?",
                (note_type, f'%"{tag}"%', limit),
            ).fetchall()
        elif note_type:
            rows = conn.execute(
                "SELECT * FROM kg_nodes WHERE type=? ORDER BY updated_at DESC LIMIT ?",
                (note_type, limit),
            ).fetchall()
        elif tag:
            rows = conn.execute(
                "SELECT * FROM kg_nodes WHERE tags LIKE ? ORDER BY updated_at DESC LIMIT ?",
                (f'%"{tag}"%', limit),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM kg_nodes ORDER BY updated_at DESC LIMIT ?", (limit,)
            ).fetchall()
        conn.close()
        return [self._row_to_dict(r) for r in rows]

    def search_nodes(self, query: str, limit: int = 10) -> list[dict]:
        """제목/요약/태그 전문 검색"""
        q = f"%{query}%"
        conn = get_connection()
        rows = conn.execute(
            """
            SELECT * FROM kg_nodes
            WHERE title LIKE ? OR summary LIKE ? OR tags LIKE ?
            ORDER BY
                CASE WHEN lower(title) LIKE lower(?) THEN 0 ELSE 1 END,
                updated_at DESC
            LIMIT ?
            """,
            (q, q, q, q, limit),
        ).fetchall()
        conn.close()
        return [self._row_to_dict(r) for r in rows]

    # ── 엣지 ──────────────────────────────────────────────

    def add_edge(self, from_id: str, to_id: str, rel_type: str = "links",
                 context: str = "", weight: float = 1.0) -> bool:
        if from_id == to_id:
            return False
        conn = get_connection()
        try:
            with conn:
                conn.execute(
                    """
                    INSERT OR IGNORE INTO kg_edges (from_id, to_id, rel_type, context, weight)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (from_id, to_id, rel_type, context, weight),
                )
            return True
        except Exception:
            return False
        finally:
            conn.close()

    def remove_edge(self, from_id: str, to_id: str, rel_type: str | None = None) -> bool:
        conn = get_connection()
        with conn:
            if rel_type:
                cur = conn.execute(
                    "DELETE FROM kg_edges WHERE from_id=? AND to_id=? AND rel_type=?",
                    (from_id, to_id, rel_type),
                )
            else:
                cur = conn.execute(
                    "DELETE FROM kg_edges WHERE from_id=? AND to_id=?",
                    (from_id, to_id),
                )
        conn.close()
        return cur.rowcount > 0

    def get_edges(self, node_id: str) -> dict:
        """노드의 모든 엣지 (outgoing + incoming)"""
        conn = get_connection()
        out_rows = conn.execute(
            """
            SELECT e.*, n.title as to_title, n.type as to_type
            FROM kg_edges e JOIN kg_nodes n ON e.to_id=n.id
            WHERE e.from_id=?
            """,
            (node_id,),
        ).fetchall()
        in_rows = conn.execute(
            """
            SELECT e.*, n.title as from_title, n.type as from_type
            FROM kg_edges e JOIN kg_nodes n ON e.from_id=n.id
            WHERE e.to_id=?
            """,
            (node_id,),
        ).fetchall()
        conn.close()
        return {
            "outgoing": [dict(r) for r in out_rows],
            "incoming": [dict(r) for r in in_rows],
        }

    def get_neighbors(self, identifier: str, hops: int = 1, direction: str = "both") -> list[dict]:
        """BFS로 N홉 이내 이웃 노드 반환"""
        node = self.get_node(identifier)
        if not node:
            return []
        start_id = node["id"]

        visited = {start_id}
        queue = deque([(start_id, 0)])
        results = []

        conn = get_connection()
        while queue:
            cur_id, depth = queue.popleft()
            if depth >= hops:
                continue

            neighbors = []
            if direction in ("out", "both"):
                rows = conn.execute(
                    "SELECT to_id, rel_type FROM kg_edges WHERE from_id=?", (cur_id,)
                ).fetchall()
                neighbors += [(r["to_id"], r["rel_type"], "→") for r in rows]
            if direction in ("in", "both"):
                rows = conn.execute(
                    "SELECT from_id, rel_type FROM kg_edges WHERE to_id=?", (cur_id,)
                ).fetchall()
                neighbors += [(r["from_id"], r["rel_type"], "←") for r in rows]

            for nid, rel_type, direction_sym in neighbors:
                if nid not in visited:
                    visited.add(nid)
                    row = conn.execute("SELECT * FROM kg_nodes WHERE id=?", (nid,)).fetchone()
                    if row:
                        d = self._row_to_dict(row)
                        d["rel_type"] = rel_type
                        d["direction"] = direction_sym
                        d["hop"] = depth + 1
                        results.append(d)
                    queue.append((nid, depth + 1))
        conn.close()
        return results

    # ── 전체 그래프 덤프 (시각화용) ───────────────────────

    def dump_graph(self) -> tuple[list[dict], list[dict]]:
        conn = get_connection()
        nodes = [self._row_to_dict(r) for r in conn.execute("SELECT * FROM kg_nodes").fetchall()]
        edges = [dict(r) for r in conn.execute("SELECT * FROM kg_edges").fetchall()]
        conn.close()
        return nodes, edges

    # ── 마크다운 파일 → 노드 동기화 ──────────────────────

    def sync_file(self, filepath: Path, vault_path: Path) -> str | None:
        """단일 마크다운 파일을 DB에 동기화. 노드 id 반환"""
        try:
            text = filepath.read_text(encoding="utf-8", errors="ignore")
        except Exception:
            return None

        parsed = parse_markdown(text, filepath)
        title = parsed["title"]
        if not title:
            return None

        rel_path = str(filepath.relative_to(vault_path))
        nid = self.add_node(
            title=title,
            note_type=parsed["type"] if parsed["type"] in NODE_TYPES else "concept",
            tags=parsed["tags"],
            summary=parsed["summary"],
            path=rel_path,
            vault_path=str(vault_path),
            node_id=parsed["id"],
        )
        return nid

    def resolve_links(self, vault_path: Path):
        """마크다운 wikilinks를 파싱해 kg_edges에 반영"""
        conn = get_connection()
        # 기존 자동 links 엣지 제거 (수동 엣지는 유지)
        with conn:
            conn.execute("DELETE FROM kg_edges WHERE rel_type='links'")
        conn.close()

        md_files = [p for p in vault_path.rglob("*.md") if "_templates" not in p.parts]
        for f in md_files:
            try:
                text = f.read_text(encoding="utf-8", errors="ignore")
            except Exception:
                continue
            parsed = parse_markdown(text, f)
            from_id = parsed["id"]
            if not from_id:
                continue

            # from_id가 DB에 있는지 확인
            conn = get_connection()
            exists = conn.execute("SELECT id FROM kg_nodes WHERE id=?", (from_id,)).fetchone()
            conn.close()
            if not exists:
                continue

            for link_title in parsed["links"]:
                to_node = self.get_node(link_title.strip()) or self.get_node(_slugify(link_title.strip()))
                if to_node:
                    self.add_edge(from_id, to_node["id"], "links")

    # ── 노트 파일 생성 ────────────────────────────────────

    def create_note_file(self, title: str, content: str, note_type: str,
                         tags: list, links: list, vault_path: Path) -> Path:
        """마크다운 파일 생성 후 DB 동기화. 파일 경로 반환"""
        subdir = NODE_TYPE_DIRS.get(note_type, note_type)
        target_dir = vault_path / "docs" / subdir
        target_dir.mkdir(parents=True, exist_ok=True)

        slug = _slugify(title)
        filepath = target_dir / f"{slug}.md"

        fm_str = build_frontmatter(title, note_type, tags, links)
        # content가 자체 frontmatter 블록을 포함하면 제거 (이중 블록 방지)
        stripped = re.sub(r"^---\s*\n.*?\n---\s*\n", "", content, count=1, flags=re.DOTALL).lstrip()
        full_text = fm_str + stripped

        filepath.write_text(full_text, encoding="utf-8")
        self.sync_file(filepath, vault_path / "docs")
        return filepath

    # ── 프로젝트 상태 업데이트 ────────────────────────────

    def update_node_progress(
        self,
        node_id: str,
        summary: str,
        progress: str = "",
        open_intents: str = "",
    ) -> bool:
        """노드의 summary/progress를 업데이트하고 .md 파일도 동기화한다.

        - summary: 현재 상태 한두 문장
        - progress: 상세 진행 내용 (## Progress 섹션에 기록)
        - open_intents: 다음 세션에 이어할 작업
        """
        node = self.get_node(node_id)
        if not node:
            return False

        # 1) SQLite 업데이트
        conn = get_connection()
        with conn:
            conn.execute(
                "UPDATE kg_nodes SET summary=?, updated_at=datetime('now','localtime') WHERE id=?",
                (summary[:500], node_id),
            )
        conn.close()

        # 2) .md 파일 업데이트 (존재하는 경우)
        vault_root = node.get("vault_path", "")
        rel_path = node.get("path", "")
        if vault_root and rel_path:
            md_path = Path(vault_root) / rel_path
            if md_path.exists():
                self._patch_md_progress(md_path, summary=summary, progress=progress, open_intents=open_intents)

        return True

    def _patch_md_progress(self, md_path: Path, summary: str, progress: str, open_intents: str):
        """마크다운 파일의 frontmatter summary와 ## Progress 섹션을 갱신한다."""
        try:
            text = md_path.read_text(encoding="utf-8", errors="ignore")
        except Exception:
            return

        # frontmatter summary 교체
        def _replace_summary(m: re.Match) -> str:
            fm_text = m.group(1)
            if re.search(r"^summary:", fm_text, re.MULTILINE):
                fm_text = re.sub(
                    r"^summary:.*$",
                    f"summary: {summary}",
                    fm_text,
                    flags=re.MULTILINE,
                )
            else:
                fm_text = fm_text.rstrip() + f"\nsummary: {summary}\n"
            return f"---\n{fm_text}\n---\n"

        text = _FRONTMATTER_RE.sub(_replace_summary, text, count=1)

        # ## Progress 섹션 갱신 또는 추가
        now = datetime.now().strftime("%Y-%m-%d %H:%M")
        progress_lines = [f"\n## Progress\n\n> 마지막 업데이트: {now}\n"]
        if progress:
            progress_lines.append(f"\n{progress.strip()}\n")
        if open_intents:
            progress_lines.append(f"\n### 다음 작업\n\n{open_intents.strip()}\n")
        new_progress_block = "".join(progress_lines)

        progress_pattern = re.compile(r"\n## Progress\b.*?(?=\n## |\Z)", re.DOTALL)
        if progress_pattern.search(text):
            text = progress_pattern.sub(new_progress_block, text)
        else:
            text = text.rstrip() + "\n" + new_progress_block

        try:
            md_path.write_text(text, encoding="utf-8")
        except Exception:
            pass

    # ── 내부 헬퍼 ─────────────────────────────────────────

    @staticmethod
    def _row_to_dict(row) -> dict:
        d = dict(row)
        try:
            d["tags"] = json.loads(d.get("tags", "[]"))
        except Exception:
            d["tags"] = []
        return d


# 싱글턴
_kg_instance: KnowledgeGraph | None = None


def get_kg() -> KnowledgeGraph:
    global _kg_instance
    if _kg_instance is None:
        _kg_instance = KnowledgeGraph()
    return _kg_instance

