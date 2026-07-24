"""현재 DB의 project scope_key 현황 조회"""

import sqlite3
import yaml
from pathlib import Path

cfg_path = Path.home() / ".engram" / "user.config.yaml"
with open(cfg_path) as f:
    cfg = yaml.safe_load(f)
db_root = cfg.get("db", {}).get("root_dir", "D:/intel_engram")
db_path = Path(db_root) / "engram.db"
print("DB path:", db_path)

conn = sqlite3.connect(db_path)
conn.row_factory = sqlite3.Row

print("\n--- sessions scope_key (project:) ---")
rows = conn.execute("SELECT scope_key, COUNT(*) as cnt FROM sessions WHERE scope_key LIKE 'project:%' GROUP BY scope_key").fetchall()
for r in rows:
    print(dict(r))

print("\n--- working_memory scope_key ---")
rows = conn.execute("SELECT scope_key FROM working_memory WHERE scope_key LIKE 'project:%'").fetchall()
for r in rows:
    print(dict(r))

print("\n--- memories table columns ---")
cols = conn.execute("PRAGMA table_info(memories)").fetchall()
for c in cols:
    print(c[1], c[2])  # name, type

print("\n--- memories project scope (first 5) ---")
try:
    rows = conn.execute("SELECT * FROM memories LIMIT 5").fetchall()
    if rows:
        print("columns:", list(rows[0].keys()))
        for r in rows:
            print({k: v for k, v in dict(r).items() if k in ("id", "project", "project_key", "scope", "source", "provider")})
except Exception as e:
    print("ERR:", e)

print("\n--- KG nodes type=project ---")
rows = conn.execute("SELECT id, title FROM kg_nodes WHERE type='project' LIMIT 20").fetchall()
for r in rows:
    print(dict(r))

conn.close()
