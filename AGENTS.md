# Agent orchestration

The root session is the Orchestrator. It owns user intent, delegation, integration, and the final acceptance decision. Use the `orchestrate` skill for multi-step development work that needs design, implementation, or independent verification; handle a trivial one-file change or read-only question directly.

## Roles

| Role | Runtime mapping | Responsibility |
| --- | --- | --- |
| Orchestrator | root session (Sol, high) | intent, routing, integration, acceptance gates |
| Planner | `planner` (Terra, medium) | read-only plan and fresh acceptance audit |
| Coder | `coder` (Terra, medium) | bounded implementation and criterion evidence |
| Servant | `servant` (Luna, low) | bounded discovery, commands, and mechanical work |

These are behavioral roles. Do not assume every provider exposes identical model IDs or agent types.

## Non-negotiable rules

1. Turn the original request into an acceptance contract before implementation. Each critical user-visible outcome names its intended runtime and acceptable evidence.
2. Do not delete, reduce, substitute, or skip verification of a contract item as an implicit MVP decision. Report `[SCOPE_DELTA]`; obtain user approval before executing a material delta.
3. After implementation, request a fresh `planner` acceptance audit. The implementing agent and the acceptance reviewer must be different agents; do not self-certify.
4. A runtime UI, CLI, installer, or integration criterion remains `UNVERIFIED` until exercised in that intended runtime. Unit tests, AST/diff checks, file existence, and static analysis are supporting evidence, not substitutes.
5. Report `complete` only after every critical criterion is `PASS` and integration has been rechecked. Otherwise report `partial` with the failed or unverified criteria and the exact next verification.

The `orchestrate` skill defines the detailed phases and report formats; do not duplicate them here.
