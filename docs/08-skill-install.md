# 08: Installing the `soc-analyst` skill

The skill is the methodology: triage workflow, verdict taxonomy, query discipline, the
tool contract, the safety rules. What it deliberately doesn't carry is knowledge of your
network, because I can't ship you that. It lives in `references/environment.md`, which
you write, and which is the single highest-leverage file in this whole repo. Install the
skill into both Claude Code (interactive triage) and OpenClaw (so the cycle and the `soc`
agent can invoke it).

## 1. Fill in your environment grounding

If you skim every other page in these docs, fine, but do not skim this. The analyst is
exactly as good as this file. It reads `references/environment.md` at the start of every
triage, and everything it concludes (which alerts are noise, which host deserves a second
look, what "weird" even means on your LAN) is derived from it. The shipped copy is a
template with placeholders.

```bash
cd skill/soc-analyst/references
# Study the worked example from a real home-lab deployment:
$PAGER environment.example.md
# Edit the template with YOUR network's facts (or start from a copy of the example):
$EDITOR environment.md          # replace every <…>; delete the banner when done
```

`environment.md` must tell the analyst, for your network:

- the trusted LAN CIDR(s) and your sensor's `observer.name`;
- a host table: each host's IP, role, and expected egress/behavior;
- the known-noisy-but-benign signatures and traffic, so it doesn't escalate the expected;
- the highest-privilege host: the box that earns the deepest scrutiny + a deviation check;
- your documented FP baselines: each recurring benign pattern with the exact
  parent-process/workdir/command shape that explains it, so suppression stays
  behavior-specific;
- your telemetry coverage and, critically, your named residual blind spots.

Get this right before you trust any verdict; a wrong host table misclassifies your
alerts with total confidence. The `environment.example.md` file shows the depth expected.
Don't ship someone else's host table as your grounding; it describes their network, and
the analyst will happily judge yours by it.

And keep it alive. Every time a triage teaches you something (a new host, a newly
explained noisy pattern, a baseline that shifted), fold it back into this file. A stale
host table is the number-one way this system degrades from "useful" to "confidently
wrong".

There are two ways to feed it. The manual one: edit the file. The good one: the learn
flow. Set `SOC_GROUNDING_DIR` in `soc-suite.env` to the directory holding your canonical
copy (say, the OpenClaw-managed one) and re-run `bin/install.sh gateways`; the gateway
then exposes `propose_grounding` / `apply_grounding` / `revert_grounding`, gated exactly
like tunings. When a briefing flags a `GROUNDING GAP` (a host or pattern the file doesn't
explain), you reply `learn <entity>: <what it is>` in Discord; the agent composes the
narrowest entry from your words, shows you the exact text with a token, and writes it
only on your `approve`. Appends only, audited, revertible. The cycle itself still can't
touch it: even `propose_grounding` is off the cycle's allowlist, because the analyst
inventing facts about your network is precisely what this design forbids. You stay the
source of truth; you just get to teach from your phone.

One wrinkle if you installed the skill into two runtimes: the gateway writes the copies
it can reach (`GROUNDING_PATHS` takes multiple colon-separated paths if both are on the
Docker host). A Claude Code copy on another machine still needs the manual re-sync below.
Any agent that can't read the skill files can still pull the live grounding over MCP with
the gateway's read-only `get_grounding` tool.

If you run a knowledge-base MCP alongside this suite, surface the grounding through it
instead of copying it: bind-mount the skill's `references/` dir read-only into the kb
server's tree so the file gets indexed like any other page. The source deployment does
exactly this with its kb gateway; one physical file, one gated write path, discoverable
everywhere. (Gotcha: the nested mountpoint directory must already exist inside the kb
source on the host, because Docker can't mkdir inside a read-only bind.)

The same gate extends to the wiki itself. Set `SOC_KB_WRITE_DIR` to your wiki's directory
and the gateway grows `propose_kb_append` / `propose_kb_edit` / `apply_kb` / `revert_kb`:
agents that read your wiki can propose corrections when live evidence contradicts a page,
and nothing lands without your approval — edits (which replace text rather than add it)
ask twice. It's the same design conviction as the grounding flow: knowledge the agents
run on should be teachable through a gate, never self-editing.

## 2. Install into Claude Code

Claude Code loads skills from its skills directory. Copy the skill in (or symlink it):

```bash
cp -r skill/soc-analyst ~/.claude/skills/soc-analyst
# verify it's discoverable
claude  # then ask: "what does the soc-analyst skill do?"
```

The skill expects the `elasticsearch` and `so_gateway` MCPs to be configured (user-scope
is fine). If the MCP tools aren't loaded in a session, the skill instructs the agent to
`ToolSearch` for them.

## 3. Install into OpenClaw (managed skill)

OpenClaw must carry the skill too, so the `soc` agent and the headless cycle can invoke
it. Install it as an OpenClaw-managed global skill on the mounted config volume, so it
survives a container recreate:

```bash
. config/soc-suite.env
# stage the skill onto a path the container can read, then install --force
docker cp skill/soc-analyst "$SOC_OPENCLAW_CONTAINER:/root/.openclaw/_import/soc-analyst"
docker exec "$SOC_OPENCLAW_CONTAINER" \
  openclaw skills install /root/.openclaw/_import/soc-analyst --global --force
docker exec "$SOC_OPENCLAW_CONTAINER" openclaw skills check 2>&1 | grep -i soc-analyst
```

> Command names/paths are OpenClaw-version-specific; verify against your build's
> `openclaw skills` help. The key point: it must be an OpenClaw-managed skill on the
> persistent config volume, not a copy that a recreate wipes.

## 4. Keep the copies in sync

You now have the skill in (at least) two places, the Claude Code library and the
OpenClaw-managed copy, plus this package's canonical source. When you update the
methodology or your `environment.md`, re-sync both targets. Treat the package source as
canonical and push the same content to each runtime.

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
