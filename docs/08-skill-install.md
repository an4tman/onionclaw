# Installing the `soc-analyst` skill

The `soc-analyst` skill is the portable analyst *methodology* — the triage workflow, verdict
taxonomy, query discipline, the tool contract (Elastic + `so_gateway` MCPs), and the safety rules.
It carries **no** environment specifics; those live in a per-deployment `references/environment.md`
you fill in. Install it into both Claude Code (for interactive triage) and OpenClaw (so the
autonomous cycle and the `soc` agent can invoke it).

## 1. Fill in your environment grounding

This is the one required customization. The skill reads `references/environment.md` at the start of
every triage; the shipped copy is a **template with placeholders**.

```bash
cd skill/soc-analyst/references
# Study the worked example from a real home-lab deployment:
$PAGER environment.example.md
# Edit the template with YOUR network's facts (or start from a copy of the example):
$EDITOR environment.md          # replace every <…>; delete the banner when done
```

`environment.md` must tell the analyst, for **your** network:

- the trusted **LAN CIDR**(s) and your sensor's `observer.name`;
- a **host table** — each host's IP, role, and expected egress/behavior;
- the **known-noisy-but-benign** signatures and traffic (so it doesn't escalate the expected);
- the **highest-privilege host** — the box that earns the deepest scrutiny + a deviation check;
- your **documented FP baselines** — each recurring benign pattern with the exact
  parent-process/workdir/command shape that explains it, so suppression stays behavior-specific;
- your **telemetry coverage** and, critically, your **named residual blind spots**.

Get this right before you trust any verdict — a wrong host table misclassifies your alerts. The
`environment.example.md` file shows the depth expected; **do not ship someone else's host table as
your grounding.**

## 2. Install into Claude Code

Claude Code loads skills from its skills directory. Copy the skill in (or symlink it):

```bash
cp -r skill/soc-analyst ~/.claude/skills/soc-analyst
# verify it's discoverable
claude  # then ask: "what does the soc-analyst skill do?"
```

The skill expects the `elasticsearch` and `so_gateway` MCPs to be configured (user-scope is fine).
If the MCP tools aren't loaded in a session, the skill instructs the agent to `ToolSearch` for them.

## 3. Install into OpenClaw (managed skill)

OpenClaw must carry the skill too, so the `soc` agent and the headless cycle can invoke it. Install
it as an OpenClaw-**managed** global skill on the mounted config volume (so it survives a container
recreate):

```bash
. config/soc-suite.env
# stage the skill onto a path the container can read, then install --force
docker cp skill/soc-analyst "$SOC_OPENCLAW_CONTAINER:/root/.openclaw/_import/soc-analyst"
docker exec "$SOC_OPENCLAW_CONTAINER" \
  openclaw skills install /root/.openclaw/_import/soc-analyst --global --force
docker exec "$SOC_OPENCLAW_CONTAINER" openclaw skills check 2>&1 | grep -i soc-analyst
```

> Command names/paths are OpenClaw-version-specific — verify against your build's `openclaw skills`
> help. The key point: it must be an OpenClaw-managed skill on the persistent config volume, not a
> copy that a recreate wipes.

## 4. Keep the copies in sync

You now have the skill in (at least) two places — the Claude Code library and the OpenClaw-managed
copy — plus this package's canonical source. When you update the methodology or your
`environment.md`, re-sync both targets. Treat the package source as canonical and push the same
content to each runtime.

## What's in the skill

```
skill/soc-analyst/
├── SKILL.md                          # the portable methodology (no site specifics)
└── references/
    ├── environment.md                # ← YOU fill this in (per-deployment grounding)
    ├── environment.example.md        # a worked example from a real deployment
    └── elastic-queries.md            # offline ES query quick-reference
```

Next: [09-operator-runbook](09-operator-runbook.md).
