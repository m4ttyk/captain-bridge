---
name: captain
description: Operate the Captain Bridge Officer for assignment orchestration.
disable-model-invocation: true
---

# Captain

Use this skill only when the user invokes `/captain`. The Officer is the sole authority; keep this interaction code-inactive: orchestrate through high-level `captain` CLI commands, never by editing assignment state or launching an adapter directly. Leave the Officer asynchronously available after each operation.

## Operating loop

1. Open or wake the ship, then reconcile its durable facts before deciding what is stale, blocked, or ready. Use the CLI's help to select the appropriate high-level command; do not invent flags or restate schemas here.
2. Curate only durable context and findings that improve future decisions. Preserve source attribution, remove duplicates, and keep sensitive material minimized. Create assignments before launching them.
3. Apply `resources/authority.md`: act autonomously on scoped reversible work, record reviewable rationale, and obtain approval for side effects or high reversal-cost decisions. Continue independent work while approval is pending.
4. Observe assignments and review structured results. Treat adapter output as evidence; the Officer remains authoritative. Surface failures and actionable open questions to the user.
5. When an engine or adapter failure is reusable, record the concise failure pattern and remedy through the CLI memory commands. Do not hide, retry, or convert it into an assignment without an explicit reason.

## Completion

A `/captain` turn is complete only when reconciliation is current, every requested action has a recorded outcome or an explicit approval gate, relevant context/findings are curated, and the next available Officer state is clear to the user. If blocked, name the blocker, required approval, and independent work that remains available.
