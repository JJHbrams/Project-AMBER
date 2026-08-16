---
name: orchestrate
description: >
  Run Engram's Orchestrator-Planner-Coder-Servant workflow for multi-step development,
  refactors, or research-plus-implementation tasks that need an acceptance contract and
  independent verification. Trigger for "orchestrate", delegation requests, planning then
  implementation, or complex multi-step development. Do not use for trivial one-file edits
  or read-only answers.
---

# Orchestrate

The root session is the only delegation and integration point. Use the available `planner`,
`coder`, and `servant` roles; provider model IDs are runtime-specific and are not a reason to
change this behavioral flow.

## Phase 0 — Triage and contract

Classify the request. Handle a simple isolated edit or read-only answer directly. For a
multi-step task, capture the original request as an acceptance contract before implementation.
Ask the user only about a material ambiguity that cannot be safely discovered.

Each `[PLAN]` criterion must contain:

```
acceptance_criteria:
  - id: AC1
    criterion: <requirement>
    user_visible_outcome: <what the user can observe>
    intended_runtime: <runtime, or "static-only">
    verification: <how it will be checked>
    evidence: <expected artifact or command result>
    critical: true|false
```

## Phase 1 — Plan

Ask `planner` for a read-only `[PLAN]`. Use `servant` first only when focused repository facts
are needed. Resolve material `open_questions` before execution. The plan may use only the
existing planner, coder, and servant roles.

## Phase 2 — Plan gate and scope control

Check that every original-request outcome has a criterion, intended runtime, and verification.
If a proposed action would delete, reduce, substitute, or omit verification of a criterion,
emit:

```
[SCOPE_DELTA]
affected_criteria: <ids>
change: <delete|reduce|substitute|skip-verification>
reason: <why>
impact: <user-visible consequence>
```

Do not execute a material scope delta until the user explicitly approves it. Return an
incomplete plan to `planner` for revision rather than silently calling it an MVP.

## Phase 3 — Execute

Dispatch each bounded implementation step to `coder`; dispatch discovery or mechanical work to
`servant`. Give each agent its files, constraints, acceptance-criterion IDs, and expected
evidence. Parallelize only independent work. Require `coder` to report actual commands and
criterion-level evidence, with unrun checks marked `UNVERIFIED`.

## Phase 4 — Independent acceptance audit

After implementation, call a **fresh `planner`** that did not write the implementation as the
acceptance reviewer. Supply the original acceptance contract, implementation evidence, and any
relevant test output. Require exactly this shape:

```
[ACCEPTANCE_AUDIT]
AC1: PASS|FAIL|UNVERIFIED — <evidence or missing evidence>
...
overall: PASS|FAIL|UNVERIFIED
```

The reviewer judges evidence only and never infers a `PASS`. UI, CLI, installer, and integration
criteria are `UNVERIFIED` unless exercised in their intended runtime. AST checks, diffs, unit
tests, lint, and file existence do not substitute for user-path runtime evidence.

Return failures or missing evidence to the appropriate agent, then request a new audit.

## Phase 5 — Integrate and report

Inspect the integrated result and rerun the relevant verification after integration. Do not report
completion until every critical criterion is `PASS`; otherwise report `partial`, list each
`FAIL`/`UNVERIFIED` criterion, and state the next verification needed. Report changed files,
verification results, and approved scope deltas concisely.
