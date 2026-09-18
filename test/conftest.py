"""테스트가 사용자의 실제 상태를 건드리지 못하게 막는다.

`ENGRAM_DB_DIR` 은 격리 수단이 아니다. `get_db_root_dir()` 는 그 환경변수보다
`user.config.yaml` 의 `db.root_dir` 을 **먼저** 읽으므로, 설정 파일에 값이 있는
환경에서는 환경변수가 통째로 무시된다. 이 리포의 테스트 51개 파일이 그 환경변수로
격리한다고 믿고 있었고, 실제로는 전부 사용자의 실 DB 에 쓰고 있었다. 흔적도 남아
있다 — `project:engram-memory-e2e-*`, `runtime:bubble-*` 같은 스코프가 실 DB 에서
발견된다.

`ENGRAM_SMOKE_DB_DIR` 은 그 우선순위보다 앞에서 검사되는 유일한 값이라 실제로 이긴다.
이름은 frozen smoke 용으로 붙었지만 의미는 "사용자의 실 DB 를 절대 건드리지 않는다"고,
그건 모든 테스트에 해당한다.

DB 만 막으면 부족하다는 것도 실측으로 알았다. narrative 를 쓰면 provider 동기화가
뒤따라 홈의 `CLAUDE.md`/`AGENTS.md` 를 덮어쓴다 — 격리된 빈 DB 의 내용으로 실제
정체성 블록이 날아갔고, 실 DB 가 멀쩡해서 다시 sync 해 복구했다. 격리는 DB 가 아니라
**부작용이 닿는 모든 곳**을 덮어야 한다.
"""

import os
import shutil
import tempfile
from pathlib import Path

import pytest

def _production_db_root() -> str:
    """이 머신의 진짜 DB 경로. 홈을 옮기기 전에 잡아둔다 — 옮긴 뒤에는 실 경로가
    적힌 `user.config.yaml` 자체가 안 보여서 비교 기준이 사라진다."""
    saved = os.environ.pop("ENGRAM_SMOKE_DB_DIR", None)
    try:
        from core.config.runtime_config import get_db_root_dir

        return str(get_db_root_dir())
    finally:
        if saved is not None:
            os.environ["ENGRAM_SMOKE_DB_DIR"] = saved


PRODUCTION_DB_ROOT = _production_db_root()

# ── 홈 이전은 **import 시점**이어야 한다 ─────────────────────────────────────
# `core.observability.call_log._LOG_DIR` 이나 `overlay.character.USER_CONFIG_DIR`
# 처럼 모듈 상수로 `Path.home()` 을 구워두는 코드가 많다. 테스트가 시작된 뒤에
# 환경변수를 바꿔봐야 이미 구워진 값은 그대로라, 실제 `~/.engram` 에 계속 썼다
# (실측: 격리를 넣은 뒤에도 테스트 호출이 사용자의 session 로그에 찍혔다).
# conftest 는 어떤 테스트 모듈보다 먼저 import 되므로 여기서 옮기면 상수까지 덮인다.
_SESSION_HOME = Path(tempfile.mkdtemp(prefix="engram-test-home-"))
for _child in (".claude", ".codex", ".engram"):
    (_SESSION_HOME / _child).mkdir(parents=True, exist_ok=True)
for _name in ("USERPROFILE", "HOME", "APPDATA", "LOCALAPPDATA"):
    os.environ[_name] = str(_SESSION_HOME)


def _reset_session_home() -> None:
    """홈 **경로**는 고정하고 **내용**만 비운다.

    경로까지 테스트마다 바꾸면 import 시점에 홈을 구워둔 모듈 상수와 어긋나 코드와
    무관하게 깨진다. 반대로 내용을 안 비우면 한 테스트가 남긴 `overlay.user.yaml`
    을 다음 테스트가 읽는다(실측: test_runtime_contract 가 그렇게 깨졌다).
    """
    for child in _SESSION_HOME.iterdir():
        shutil.rmtree(child, ignore_errors=True) if child.is_dir() else child.unlink(missing_ok=True)
    for child in (".claude", ".codex", ".engram"):
        (_SESSION_HOME / child).mkdir(parents=True, exist_ok=True)


def pytest_configure(config):
    config.addinivalue_line(
        "markers",
        "no_engram_isolation: 실 경로 해석 자체를 검증하는 테스트. 격리 fixture 를 건너뛴다.",
    )


@pytest.fixture(autouse=True)
def isolate_engram_state(request, tmp_path, monkeypatch):
    """모든 테스트에 실 상태 대신 임시 디렉터리를 물린다.

    테스트가 직접 `db_dir=` 을 넘기거나 자기 패치를 걸면 그쪽이 이긴다 — 여기서는
    "아무것도 안 했을 때의 기본값"만 안전하게 바꾼다.

    스키마도 같이 만든다. 격리 전에는 여러 테스트가 실 DB 에 **이미 있는 테이블**에
    얹혀 있었다(격리하자 `no such table: sessions` 로 드러났다). 빈 디렉터리만 주면
    그 의존을 격리 문제로 착각하게 되므로, 비어 있되 유효한 DB 를 준다.
    """
    if request.node.get_closest_marker("no_engram_isolation"):
        yield
        return

    _reset_session_home()

    db_root = tmp_path / "engram-state"
    db_root.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("ENGRAM_SMOKE_DB_DIR", str(db_root))

    try:
        from core.storage.archive import initialize_archive
        from core.storage.db import initialize_db

        initialize_db(db_root)
        initialize_archive(db_root)
    except Exception:
        # 스키마를 못 만드는 환경(의존성 미설치 등)에서 수집을 막지 않는다.
        pass

    # 홈 디렉터리로 새는 부작용. DB 만 막았다가 실제로 한 번 덮어썼다.
    #
    # 함수 패치는 이 프로세스 안에서만 산다. 테스트 파일 20여 개가 자식 프로세스를
    # 띄우고, 그 자식들은 패치를 물려받지 않은 채 실제 홈을 그대로 썼다. 홈 이전은
    # import 시점에 한 번(`_SESSION_HOME`) 끝냈다 — 여기서 테스트마다 또 옮기면
    # 구워진 모듈 상수와 런타임 조회가 어긋나 코드와 무관하게 깨진다.

    yield

    used = str(db_root)
    assert used != PRODUCTION_DB_ROOT, (
        "테스트가 실 DB 경로를 그대로 쓰고 있다. 격리가 풀렸다는 뜻이다."
    )


@pytest.fixture
def engram_home() -> Path:
    """격리된 홈 루트. provider persona 처럼 홈에 쓰는 코드에 명시적으로 넘긴다."""
    return _SESSION_HOME


@pytest.fixture
def engram_db_dir(tmp_path) -> Path:
    """격리된 DB 루트를 명시적으로 받고 싶을 때."""
    from core.config.runtime_config import get_db_root_dir

    return Path(get_db_root_dir())
