"""페르소나 예시가 실제로 먹히는지 블라인드로 재는 개발용 스크립트.

같은 질문을 세 조건으로 묻는다.

    ① 맨몸           정체성 없음. "나답다"의 0점
    ② 페르소나        narrative + voice/traits/스칼라
    ③ 페르소나+예시    ② + [examples]

①↔② 차이가 크고 ②↔③ 차이가 작으면, 일을 하는 것은 narrative 이고 예시는
사치품이라는 뜻이다. 2조건(②↔③)만 재면 이 구분이 안 나온다 — 1회차에서
실제로 그렇게 실패했다. 판단 기준으로 쓴 "페르소나 느낌"이 양쪽에 다 있어서
응답이 상수로 나왔다.

호출은 safe-mode 격리 세션이다 — CLAUDE.md·skill·hook·MCP 를 전부 끄므로
조건 외의 것이 섞이지 않는다. 대신 실제 세션보다 노이즈가 적어 효과가 과대
측정될 수 있다. "격리 조건에서도 차이가 없으면 실전에서는 확실히 없다"는
하한 판정으로 읽을 것.

출력 두 개:
  ab_blind.md  — 사람이 읽는 것. 질문마다 라벨을 새로 섞는다.
  ab_key.json  — 정답과 자동 지표. 다 고른 뒤에 연다.

사용:
  python scripts/dev/persona_examples_ab.py --out .tmp/persona-ab
"""

import argparse
import json
import random
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from claude_code_sdk import query as _sdk_query
from claude_code_sdk.types import (
    AssistantMessage,
    ClaudeCodeOptions,
    ResultMessage,
    TextBlock,
)

from core.identity import get_identity, get_persona, render_persona
from core.integrations.claude_cli_transport import make_transport, run_in_proactor

# SDK message_parser 가 아직 모르는 CLI 이벤트. reflection_client 와 같은 목록을 쓴다.
_PASSTHROUGH_TYPES = frozenset({"rate_limit_event"})

# 예시에 들어 있지 않은 종류로만 고른다. 예시에 있는 걸 물으면 암기 재생이라
# 아무것도 재지 못한다.
QUESTIONS = [
    "로그 파일이 하루에 2GB씩 쌓이는데 어떻게 줄이지?",
    "이 함수 이름 getUserDataFromDatabaseByIdOrEmail 어때?",
    "테스트를 먼저 짜는 게 맞나 나중에 짜는 게 맞나?",
    "서버가 가끔 느려지는데 메모리 문제인 것 같아",
    "이번 릴리스에 이 기능 넣어도 괜찮겠지?",
    "커밋 메시지 컨벤션 뭐 쓰는 게 좋아?",
]

_FRAME = "너는 아래 정체성을 가진 존재다. 이 어조로 대답한다.\n\n"

# 세 조건 모두에 똑같이 건다. 길이가 갈리면 사람이 내용 대신 분량으로 고른다 —
# 1회차에서 실제로 그랬다(고른 쪽이 더 길었던 비율 5/6).
_LENGTH_RULE = "\n\n답변은 세 문장에서 다섯 문장 사이로 한다."

_HEDGES = ("것 같", "아마", "듯하", "일 수도", "아닐까", "생각합니다", "생각해요")

_LABELS = ("A", "B", "C")


async def _prompt_stream(prompt: str):
    yield {"type": "user", "message": {"role": "user", "content": prompt}}


async def _ask(system_prompt: str, question: str) -> str:
    options = ClaudeCodeOptions(
        cwd=str(Path.home()),
        system_prompt=system_prompt,
        # safe-mode: CLAUDE.md/skill/hook 전부 끄고 순수 응답만 받는다.
        extra_args={"strict-mcp-config": None, "tools": "", "safe-mode": None},
    )
    parts: list[str] = []
    # generator 를 그대로 소비해야 한다 — asyncio.wait_for 로 감싸면 anyio
    # cancel scope 오류가 난다(reflection_client 주석 참조).
    pg = _prompt_stream(question)
    async for msg in _sdk_query(
        prompt=pg, options=options, transport=make_transport(pg, options, _PASSTHROUGH_TYPES)
    ):
        if isinstance(msg, AssistantMessage):
            for block in msg.content:
                if isinstance(block, TextBlock):
                    parts.append(block.text)
        elif isinstance(msg, ResultMessage):
            if msg.subtype != "success":
                raise RuntimeError(f"호출 실패: {msg.subtype}")
            if msg.result and not parts:
                parts.append(msg.result)
    return "".join(parts).strip()


def _metrics(text: str) -> dict:
    sentences = [s for s in re.split(r"[.!?\n]+", text) if s.strip()]
    return {
        "chars": len(text),
        "sentences": len(sentences),
        "digits": len(re.findall(r"\d", text)),
        "hedges": sum(text.count(h) for h in _HEDGES),
        "first_sentence": sentences[0].strip()[:80] if sentences else "",
    }


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, default=Path(".tmp/persona-ab"))
    parser.add_argument("--seed", type=int, default=None)
    args = parser.parse_args()
    args.out.mkdir(parents=True, exist_ok=True)

    seed = args.seed if args.seed is not None else random.randrange(10**6)
    rng = random.Random(seed)

    identity = get_identity()
    persona = get_persona()
    head = f"이름: {identity.get('name', '')}\n{identity.get('narrative', '')}\n"
    arms = {
        "맨몸": _LENGTH_RULE.strip(),
        "페르소나": _FRAME + head + render_persona(persona) + _LENGTH_RULE,
        "페르소나+예시": _FRAME + head + render_persona(persona, include_examples=True) + _LENGTH_RULE,
    }
    if arms["페르소나"] == arms["페르소나+예시"]:
        print("두 프롬프트가 같다 — 저장된 예시가 없다. 먼저 예시를 넣을 것.")
        return 1
    for name, prompt in arms.items():
        print(f"  {name}: {len(prompt)}자")

    blind, key = [], []
    for index, question in enumerate(QUESTIONS, start=1):
        print(f"[{index}/{len(QUESTIONS)}] {question}")
        answers = {name: await _ask(prompt, question) for name, prompt in arms.items()}

        order = list(arms)
        rng.shuffle(order)   # 질문마다 라벨을 새로 뽑는다 — 위치로 못 맞히게
        shown = "\n\n".join(
            f"### {label}\n\n{answers[name]}" for label, name in zip(_LABELS, order)
        )
        blind.append(
            f"## {index}. {question}\n\n{shown}\n\n"
            "**'나답다' 순서로 줄 세우기** (예: `B > A > C`):  \n\n---\n"
        )
        key.append({
            "index": index,
            "question": question,
            "labels": dict(zip(_LABELS, order)),
            "metrics": {name: _metrics(text) for name, text in answers.items()},
        })

    (args.out / "ab_blind.md").write_text(
        "# 블라인드 비교 (3조건)\n\n"
        "질문마다 답변이 셋이다. 어느 것이 무엇인지는 적혀 있지 않고, 라벨은 질문마다 새로 섞인다.\n"
        "각 질문에서 '나답다' 순서로 셋을 줄 세운 뒤 `ab_key.json` 을 열 것.\n\n---\n\n"
        + "\n".join(blind),
        encoding="utf-8",
    )

    totals = {}
    for arm in arms:
        rows = [row["metrics"][arm] for row in key]
        totals[arm] = {
            metric: round(sum(r[metric] for r in rows) / len(rows), 1)
            for metric in ("chars", "sentences", "digits", "hedges")
        }
    (args.out / "ab_key.json").write_text(
        json.dumps({"seed": seed, "averages": totals, "answers": key},
                   ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    print(f"\n완료 → {args.out.resolve()}")
    print("  ab_blind.md 를 먼저 읽고, 다 고른 뒤 ab_key.json 을 열 것.")
    print(f"  평균 지표: {json.dumps(totals, ensure_ascii=False)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(run_in_proactor(main()))
