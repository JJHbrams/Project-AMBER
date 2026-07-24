"""wiki_reminder 기능 단독 테스트 스크립트"""
import os
import sys

# CUDA 비활성화 (cudnnGetVersion 충돌 방지)
os.environ["CUDA_VISIBLE_DEVICES"] = "-1"
os.environ["TOKENIZERS_PARALLELISM"] = "false"

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from core.context.context_builder import _wiki_reminder_snippet, build_system_prompt

print("=" * 50)
print("1. SemanticGraph 상태")
print("=" * 50)
from core.graph.semantic import get_semantic_graph
sg = get_semantic_graph()
print(f"  enabled: {sg.enabled}")
if sg.enabled:
    sg._ensure_cache()
    print(f"  KGNode 캐시: {len(sg._cache_ids)}개")

print()
print("=" * 50)
print("2. _wiki_reminder_snippet 테스트")
print("=" * 50)

queries = [
    "GUI 설정창 구현",
    "Tauri 창 분기 GUI 옵션",
    "메모리 저장 경험 회상",
]
for q in queries:
    result = _wiki_reminder_snippet(q, top_k=3, threshold=0.35)
    print(f"  [{q}]")
    if result:
        for line in result.split("\n"):
            print(f"    {line}")
    else:
        print("    (결과 없음 — threshold 미달)")
    print()

print("=" * 50)
print("3. build_system_prompt wiki_reminder 섹션 확인")
print("=" * 50)
prompt = build_system_prompt(user_query="Tauri GUI 창 구현")
if "wiki_reminder" in prompt:
    start = prompt.index("<ctx:wiki_reminder>")
    end = prompt.index("</ctx:wiki_reminder>") + len("</ctx:wiki_reminder>")
    print(prompt[start:end])
else:
    print("  wiki_reminder 섹션 없음 (threshold 미달)")


