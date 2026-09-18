"""과거 대화 원문에서 페르소나 예시 후보를 캐낸다.

**이 모듈은 archive.db 를 읽기만 한다.** 후보를 돌려줄 뿐 아무것도 저장하지
않는다 — 무엇이 "나다운지"는 사람이 정한다. 자동 선별을 넣는 순간 예시는
내가 고른 것이 아니라 필터가 고른 것이 되고, 그러면 처음 문제(형용사가 만든
일반적 개성)로 되돌아간다.

## pair 를 복원하지 않는 이유

처음엔 `id` 인접으로 user→assistant 쌍을 복원하려 했다. 안 된다.

여러 대화가 같은 연속체 스코프에 **동시에** 쓴다. 연속된 turn id 17984~17989 를
직접 열어 보니 여섯 개가 전부 다른 대화였다. `session_id` 는 비어 있는 행이
93.7%이고 남은 값도 7개뿐이라 한 값 안에 동시 대화가 섞인다. `source_uuid` 는
줄 단위 랜덤 UUID 라 묶이지 않는다. **archive 에는 대화를 가르는 키가 없다.**
(직접 확인, 2026-09-17)

시간 간격으로도 못 막는다 — interleaving 이라 인접 턴 간격이 애초에 작다.

그래서 여기서는 **assistant 발화 후보만** 돌려준다. 짝이 될 user 발화는 사람이
채운다. 거짓 pair 를 만드느니 반쪽을 주는 편이 낫다. 온전한 pair 가 필요하면
대화 중 즉시 저장을 쓴다 — 그때는 모델이 자기 직전 턴을 컨텍스트로 안다.
"""

import logging
from typing import Any, Dict, List, Optional

from core.memory.scope import CONTINUUM_SCOPE
from core.storage.archive import get_archive_connection

_LOG = logging.getLogger(__name__)

# 너무 짧으면 말투가 안 드러나고, 너무 길면 예시 하나가 렌더 예산을 다 먹는다.
MIN_ASSISTANT, MAX_ASSISTANT = 40, 600


_SQL = """
SELECT id, content, ts
FROM turns
WHERE scope_key = ?
  AND role = 'assistant'
  AND length(content) BETWEEN ? AND ?
"""


def mine_candidates(
    query: str = "",
    limit: int = 20,
    scope_key: str = CONTINUUM_SCOPE,
    db_dir: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """assistant 발화 후보를 최신순으로 돌려준다. **저장하지 않는다.**

    각 후보의 `prompt_text` 는 빈 문자열이고 `needs_prompt_text` 가 True 다 —
    archive 가 대화를 가르지 못해 짝을 자동으로 붙일 수 없기 때문이다(모듈
    docstring 참조). 저장 전에 사람이 채운다.

    채울 것은 가짜 사용자 발화가 아니라 **그때의 상황**이다. 그래서 제안하는
    prompt_kind 는 'situation' 이다 — 자극이 코드나 수치였다면 'source'.

    query 가 있으면 전문검색으로 좁힌다(fts5 trigram). 한국어는 3자 이상을
    권장한다 — 그 아래는 인덱스를 못 타고 전수 스캔이 된다.
    """
    sql = _SQL
    params: List[Any] = [scope_key, MIN_ASSISTANT, MAX_ASSISTANT]
    query = (query or "").strip()
    if query:
        sql += "  AND id IN (SELECT rowid FROM turns_fts WHERE turns_fts MATCH ?)\n"
        params.append(query)
    sql += "ORDER BY id DESC LIMIT ?"
    params.append(int(limit))

    conn = get_archive_connection(db_dir)
    try:
        rows = conn.execute(sql, params).fetchall()
    except Exception:
        _LOG.warning("예시 후보 채굴 실패 — 빈 목록을 돌려준다", exc_info=True)
        return []
    finally:
        conn.close()

    return [
        {
            "response_text": row["content"],
            "prompt_text": "",
            "suggested_prompt_kind": "situation",
            "needs_prompt_text": True,
            "source_turn_id": row["id"],
            "ts": row["ts"],
        }
        for row in rows
    ]
