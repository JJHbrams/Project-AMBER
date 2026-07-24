import sqlite3

conn = sqlite3.connect("D:/intel_engram/engram.db")

# 테이블 목록
print("=== tables ===")
for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name").fetchall():
    print(r[0])

print()

# ICCC_for_ARONA 텍스트 포함된 row 검색
TABLES_TO_CHECK = []
for (name,) in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall():
    cols = [r[1] for r in conn.execute(f"PRAGMA table_info({name})").fetchall()]
    text_cols = [c for c in cols if c not in ("id", "created_at", "updated_at", "expires_at")]
    TABLES_TO_CHECK.append((name, text_cols))

print("=== ICCC_for_ARONA 포함 rows ===")
for table, cols in TABLES_TO_CHECK:
    for col in cols:
        try:
            rows = conn.execute(
                f"SELECT rowid, '{col}', substr({col},1,120) FROM {table} WHERE {col} LIKE '%ICCC_for_ARONA%'"
            ).fetchall()
            for r in rows:
                print(f"[{table}.{col}] rowid={r[0]}: {r[2]}")
        except Exception as e:
            pass

conn.close()
