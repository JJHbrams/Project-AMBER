"""
프로젝트 이름 마이그레이션: ICCC_for_ARONA → Project_Engram

수행 작업:
1. 새 디렉토리 경로 기준 project_key 계산
2. DB working_memory scope_key 업데이트  ← 1회성 DB 마이그레이션
3. DB sessions scope_key 업데이트        ← 1회성 DB 마이그레이션
4. ~/.engram/user.config.yaml kg_node_map에 매핑 추가  ← 런타임 상시 참조값 (제거 금지)
   ※ kg_node_map은 resolve_kg_node_id()가 매 호출마다 읽는 설정이므로 영구 유지 필요.
      config.yaml(공유)이 아닌 user.config.yaml(PC 한정)에 기록.
5. 실행 요약 출력 (dry-run 지원)

vault 디렉토리 rename, GitHub repo rename은 수동 작업.
"""

import hashlib
import re
import sqlite3
import sys
from pathlib import Path

import yaml

DRY_RUN = "--dry-run" in sys.argv

OLD_DIR_NAME = "ICCC_for_ARONA"
NEW_DIR_NAME = "Project_Engram"
OLD_SCOPE_KEY = "project:iccc-for-arona-a6bc1f3c"
KG_NODE_ID = "iccc-for-arona"  # 기존 KG 노드 ID (변경하지 않음)


def _slugify(value: str) -> str:
    collapsed = re.sub(r"[^a-zA-Z0-9]+", "-", (value or "").strip().lower()).strip("-")
    return collapsed or ""


def compute_new_project_key(new_dir_path: Path) -> str:
    normalized = str(new_dir_path).lower()
    slug = _slugify(new_dir_path.name)
    digest = hashlib.sha1(normalized.encode("utf-8")).hexdigest()[:8]
    return f"{slug}-{digest}"


def load_db_path() -> Path:
    cfg_path = Path.home() / ".engram" / "user.config.yaml"
    with open(cfg_path, encoding="utf-8") as f:
        cfg = yaml.safe_load(f)
    db_root = cfg.get("db", {}).get("root_dir", "D:/intel_engram")
    return Path(db_root) / "engram.db"


def update_working_memory(conn: sqlite3.Connection, old_scope: str, new_scope: str) -> int:
    rows = conn.execute("SELECT scope_key FROM working_memory WHERE scope_key = ?", (old_scope,)).fetchall()
    if not rows:
        print(f"  [skip] working_memory: '{old_scope}' 없음")
        return 0
    if not DRY_RUN:
        conn.execute(
            "UPDATE working_memory SET scope_key = ? WHERE scope_key = ?",
            (new_scope, old_scope),
        )
        conn.commit()
    print(f"  [{'dry' if DRY_RUN else 'done'}] working_memory: '{old_scope}' → '{new_scope}'")
    return len(rows)


def update_sessions(conn: sqlite3.Connection, old_scope: str, new_scope: str) -> int:
    rows = conn.execute("SELECT id FROM sessions WHERE scope_key = ?", (old_scope,)).fetchall()
    if not rows:
        print(f"  [skip] sessions: '{old_scope}' 없음")
        return 0
    if not DRY_RUN:
        conn.execute(
            "UPDATE sessions SET scope_key = ? WHERE scope_key = ?",
            (new_scope, old_scope),
        )
        conn.commit()
    print(f"  [{'dry' if DRY_RUN else 'done'}] sessions {len(rows)}개: '{old_scope}' → '{new_scope}'")
    return len(rows)


def update_user_config_kg_node_map(new_project_key: str, kg_node_id: str):
    """~/.engram/user.config.yaml에 kg_node_map 매핑을 추가한다.

    config.yaml은 공유 파일이므로 PC 한정 경로 기반 매핑은 user.config.yaml에 기록.
    """
    user_cfg_path = Path.home() / ".engram" / "user.config.yaml"
    user_cfg_path.parent.mkdir(parents=True, exist_ok=True)

    existing: dict = {}
    if user_cfg_path.exists():
        with open(user_cfg_path, encoding="utf-8") as f:
            loaded = yaml.safe_load(f)
        if isinstance(loaded, dict):
            existing = loaded

    mapping = existing.setdefault("memory", {}).setdefault("scope", {}).setdefault("kg_node_map", {})
    if new_project_key in mapping:
        print(f"  [skip] user.config.yaml kg_node_map: '{new_project_key}' 이미 존재")
        return

    if not DRY_RUN:
        mapping[new_project_key] = kg_node_id
        with open(user_cfg_path, "w", encoding="utf-8") as f:
            yaml.dump(existing, f, allow_unicode=True, default_flow_style=False, sort_keys=False)

    print(f"  [{'dry' if DRY_RUN else 'done'}] user.config.yaml kg_node_map: "
          f"'{new_project_key}' → '{kg_node_id}' ({user_cfg_path})")


def main():
    print(f"{'[DRY-RUN] ' if DRY_RUN else ''}프로젝트 이름 마이그레이션 시작\n")

    # 현재 디렉토리 기준으로 새 경로 계산
    current_dir = Path(__file__).resolve().parent.parent.parent  # ICCC_for_ARONA
    new_dir = current_dir.parent / NEW_DIR_NAME
    new_project_key = compute_new_project_key(new_dir)
    new_scope_key = f"project:{new_project_key}"

    print(f"OLD dir : {current_dir}")
    print(f"NEW dir : {new_dir}")
    print(f"OLD scope_key: {OLD_SCOPE_KEY}")
    print(f"NEW scope_key: {new_scope_key}")
    print(f"KG node_id  : {KG_NODE_ID} (변경 없음)\n")

    # 1. DB 마이그레이션
    db_path = load_db_path()
    print(f"DB: {db_path}")
    conn = sqlite3.connect(db_path)

    print("\n[1] working_memory 업데이트")
    update_working_memory(conn, OLD_SCOPE_KEY, new_scope_key)

    print("\n[2] sessions 업데이트")
    update_sessions(conn, OLD_SCOPE_KEY, new_scope_key)

    conn.close()

    # 2. user.config.yaml kg_node_map
    print(f"\n[3] user.config.yaml kg_node_map")
    update_user_config_kg_node_map(new_project_key, KG_NODE_ID)

    # 3. 안내 메시지
    print("\n--- 수동 작업 (스크립트 외) ---")
    print(f"  디렉토리 rename: {current_dir.name} → {NEW_DIR_NAME}")
    print(f"    > git -C '{current_dir.parent}' mv {OLD_DIR_NAME} {NEW_DIR_NAME}")
    print(f"  vault 디렉토리 rename (선택):")
    print(f"    D:\\intel_engram\\docs\\projects\\ICCC_for_ARONA → Project_Engram")
    print(f"  GitHub repo rename: Settings → Rename repository")
    print("\nMCP 서버 재시작 필요 (새 scope_key 적용)")


if __name__ == "__main__":
    main()
