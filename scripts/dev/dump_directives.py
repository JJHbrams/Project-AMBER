import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(__file__))))

from core.storage.db import get_connection

conn = get_connection()
rows = conn.execute(
    "SELECT key, content, priority FROM directives WHERE active=1 ORDER BY priority DESC"
).fetchall()
conn.close()

for r in rows:
    print(f"=== {r['key']} (priority={r['priority']}) len={len(r['content'])} ===")
    print(r['content'])
    print()

