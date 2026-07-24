"""
STM 브로커 통합 테스트
─────────────────────
overlay.exe 없이도 동작 — STMServer를 이 스크립트가 직접 띄움.

사용법:
  python scripts/test_stm_broker.py           # 내장 서버로 테스트
  python scripts/test_stm_broker.py --live    # 이미 떠있는 overlay 대상 테스트 (port 17384)
"""

import argparse
import json
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

BASE = "http://127.0.0.1:{port}"

# ── HTTP 헬퍼 ──────────────────────────────────────────────────────────────


def get(url: str) -> dict:
    with urllib.request.urlopen(url, timeout=3) as r:
        return json.loads(r.read())


def post(url: str, body: dict) -> dict:
    data = json.dumps(body).encode()
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=3) as r:
        return json.loads(r.read())


# ── 테스트 케이스 ──────────────────────────────────────────────────────────


def run_tests(base: str) -> bool:
    ok = True

    def check(label: str, cond: bool, detail: str = ""):
        nonlocal ok
        mark = "✅" if cond else "❌"
        print(f"  {mark} {label}" + (f"  ({detail})" if detail else ""))
        if not cond:
            ok = False

    # 1. health
    print("\n[1] health check")
    h = get(f"{base}/health")
    check("status == ok", h.get("status") == "ok", str(h))

    # 2. 세션 A 시작 (wt 터미널 흉내)
    print("\n[2] 세션 A 시작 (scope=overlay)")
    sa = post(f"{base}/stm/session/start", {"scope_key": "overlay"})
    check("session_id 반환", "session_id" in sa, str(sa))
    sid_a = sa.get("session_id", 0)

    # 3. 세션 A 메시지 저장
    print("\n[3] 세션 A → 메시지 2개 저장")
    r1 = post(
        f"{base}/stm/message",
        {
            "session_id": sid_a,
            "role": "user",
            "content": "STM 브로커 테스트 메시지입니다",
            "request_id": "test-req-001",
        },
    )
    check("첫 번째 저장 ok", r1.get("status") == "ok", str(r1))

    r2 = post(
        f"{base}/stm/message",
        {
            "session_id": sid_a,
            "role": "assistant",
            "content": "네, STM 브로커가 정상 동작 중입니다",
            "request_id": "test-req-002",
        },
    )
    check("두 번째 저장 ok", r2.get("status") == "ok", str(r2))

    # 4. 중복 request_id 차단
    print("\n[4] 중복 request_id 방지 (idempotency)")
    r_dup = post(
        f"{base}/stm/message",
        {
            "session_id": sid_a,
            "role": "user",
            "content": "이건 중복이라 무시돼야 합니다",
            "request_id": "test-req-001",
        },
    )
    check("중복 무시됨", r_dup.get("status") == "duplicate_ignored", str(r_dup))

    # 5. 세션 B (VS Code 흉내) — 같은 scope 메시지 읽기
    print("\n[5] 세션 B (VS Code 흉내) → 같은 scope 메시지 조회")
    msgs = get(f"{base}/stm/messages?scope_key=overlay&limit=10")
    messages = msgs.get("messages", [])
    check("메시지 목록 반환", isinstance(messages, list), f"{len(messages)}개")
    check("저장한 메시지 포함", any("STM 브로커 테스트" in m.get("content", "") for m in messages), str([m["content"][:30] for m in messages]))

    # 6. 다른 scope는 분리
    print("\n[6] 다른 scope (vscode) 는 overlay 메시지 안 보임")
    msgs_vs = get(f"{base}/stm/messages?scope_key=vscode&limit=10")
    vs_messages = msgs_vs.get("messages", [])
    check("scope 분리 확인", not any("STM 브로커 테스트" in m.get("content", "") for m in vs_messages), f"vscode scope: {len(vs_messages)}개")

    # 7. session/close (promote 트리거)
    print("\n[7] 세션 종료 (STM promote 트리거)")
    rc = post(f"{base}/stm/session/close", {"session_id": sid_a, "scope_key": "overlay"})
    check("close 응답 ok", rc.get("status") == "ok", str(rc))

    print()
    return ok


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--live", action="store_true", help="이미 실행 중인 overlay 대상 (port 17384)")
    parser.add_argument("--port", type=int, default=17384)
    args = parser.parse_args()

    if args.live:
        base = BASE.format(port=args.port)
        print(f"🔌 Live 모드: {base} (overlay.exe 연결)")
        try:
            get(f"{base}/health")
        except Exception as e:
            print(f"❌ overlay STM 서버 응답 없음: {e}")
            sys.exit(1)
        result = run_tests(base)
    else:
        from overlay.stm_server import STMServer

        port = 17390
        server = STMServer(port=port)
        server.start()
        time.sleep(0.3)
        base = BASE.format(port=port)
        print(f"🚀 내장 서버 모드: {base}")
        try:
            result = run_tests(base)
        finally:
            server.stop()

    if result:
        print("🎉 모든 테스트 통과")
    else:
        print("💥 일부 테스트 실패")
        sys.exit(1)


if __name__ == "__main__":
    main()
