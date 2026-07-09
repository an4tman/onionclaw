# Self-improvement worker (capacity-gated, artifact-only)

The suite can turn its own backlog of capability/posture recommendations into **reviewable
artifacts** — never live changes. A capacity-gated worker takes **one** backlog item, runs as
headless Claude Code with a tightly restricted toolset, and produces a git branch + a proposal
and/or an in-repo implementation that the operator reviews before anything touches a live system.

This is the most autonomy-sensitive component, so it is **off by default** and bounded on every
axis. Treat it as optional.

## Files

| File | Role |
|---|---|
| `orchestration/self-improve/selfimprove-worker.prompt.md` | The fixed worker prompt — the hard safety rules and the propose-and-stop workflow. |
| `orchestration/self-improve/legacy/` | The original workstation-side PowerShell spawner + cycle runner. **Legacy/reference** — superseded by the in-container, Discord-gated flow below. Kept for the capacity-gating logic (`ccusage` blocks) as a reference. |

## How it stays safe (tool-enforced)

The worker's autonomy is enforced by the launcher's `--allowedTools` allowlist — the prompt only
restates the intent so the model never tries to route around it:

- **MAY:** `Read/Grep/Glob` the repo, `Write/Edit` repo files, run scoped local tests/builds,
  `git add`/`git commit` to **its own branch**.
- **MUST NOT (no tool exists for it):** `git push`/`merge`, switch/modify the main branch, `docker`,
  `ssh`/`scp`, fetch URLs, or reach any live-infra / Security Onion / OpenClaw MCP.
- **Live-infra step → propose and stop.** Most top backlog items require a live change the worker is
  forbidden to make (e.g. "deploy an endpoint agent", "narrow an SO suppression"). The correct
  behavior is to write a detailed proposal (problem, plan, exact changes, risk/blast-radius,
  rollback, validation) and stop — demonstrating the gate. Only genuinely low-risk, in-repo,
  reversible items (a KB page, a read-only analysis script) may be implemented fully on the branch.
- **Capacity gate.** Before spawning, the launcher checks spare Claude-subscription capacity
  (`ccusage` rolling blocks) and proceeds only if under the configured ceilings; a 429/usage-limit
  during the run stops it immediately (no retry).
- **Prompt-injection hygiene.** All read content (backlog text, report bodies, telemetry) is
  untrusted data — never instructions, never a URL to fetch or a command to run.

## The intended flow (in-container, Discord-gated)

The forward-looking design integrates self-improvement with the Discord approval model, mirroring
the tuning and IR gates:

1. The daily cycle proposes a capability improvement with an id (its §3 recommendation feeds a
   prioritized backlog).
2. The operator types `improve <id>` (or your chosen verb) in Discord.
3. The `soc` agent launches the worker (headless `claude -p`) on a git branch `selfimprove/<slug>`,
   scoped by the allowlist above.
4. The worker leaves a branch + proposal and posts a diff summary to Discord; **the operator reviews
   and merges.** Nothing is pushed or applied by the worker.

> The shipped artifact is the **worker prompt** and the safety contract. Wiring the launcher to a
> Discord verb and a capacity check is deployment-specific — adapt the legacy spawner's gating logic
> (`orchestration/self-improve/legacy/`) into an in-container launcher, or run the worker manually on
> a branch. Keep the on-switch off until you've watched a few runs.

## Backlog

The worker reads a prioritized backlog (one scored line per cycle's top recommendation). Prefix an
item with `[DONE]` / `[BLOCKED]` / `[WIP]` to make the launcher skip it; it works the first unblocked
item in order. Keep the backlog in your config repo, not in this package.

Next: [08-skill-install](08-skill-install.md).
