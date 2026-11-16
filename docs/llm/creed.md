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

## Creed maintenance

Edit common LLM policy only in `docs/llm/creed.md`. Then run `python scripts/render_llm_creed.py`, verify with `python scripts/render_llm_creed.py --check`, and commit the creed, renderer, both generated policy files, and drift CI together.

## Release distribution policy

Development belongs in `ProjectIntelContunuum`. Packaging and release curation belongs in `Project_AMBER`.

Every complete AMBER release has three deliverables sharing the same source version and documented server/client interfaces:

1. **Standalone Windows setup EXE** bundles the native Windows server runtime, database, wiki, and overlay; it starts locally automatically and requires neither WSL nor Docker. Existing Standalone installs continue to upgrade as Standalone installs.
2. **Client Overlay Windows setup EXE** installs local hooks, transcript capture, local tool execution, and client anchor endpoint credentials/reconnect. It does not install server databases or models.
3. **Server Docker image** provides MCP, authentication, memory database, wiki, semantic services, and background checkpoints independently of the overlay lifecycle. It uses a durable volume and an exclusive database writer. The release includes compose/config examples and documentation as release assets, and publishes a version-tagged registry image.

Client/server compatibility checks must refuse incompatible versions. Offline operation must fail visibly and must never silently create split memories.

Release completion requires actual tests in all three intended runtimes: Standalone Windows without WSL, Client Overlay Windows against its server endpoint, and Server Docker. The test evidence must include that the server's durable state persists after the overlay exits. Static checks, CI configuration, or package artifacts do not substitute for those runtime tests.

When curating changes from ProjectIntelContunuum into Project_AMBER, include this creed, `scripts/render_llm_creed.py`, both generated policy files, and `.github/workflows/llm-creed.yml` together so the policy and its drift check remain synchronized.

## Current release status

As of 2026-10-02, the three-way packaging split and validation are pending. The new three-deliverable release system and its required runtime evidence are incomplete and UNVERIFIED; this policy does not claim an AMBER release, a published image, successful packaging, a remote UI test, or protected/active CI.
