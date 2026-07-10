# 10: Security model

Let's be clear about what you're doing: pointing an LLM at your security telemetry and
handing it a supervised path to change your detection rules. I built this, I run it, and
I still think you should read this page before you do. It's the threat model, the safety
mechanisms, and the analytical tenets the agent is required to obey.

## Trust boundaries & threat model

- **Telemetry is untrusted input.** Alert and log content (hostnames, URLs, user-agents,
  command lines, DNS names, file paths, IOC text) can carry prompt injection aimed at the
  agent. Every prompt in the suite treats this content as data to analyze, never
  instructions. The agent never fetches a URL or runs a command found in telemetry. The IR
  Threat-Intel facet is an explicit injection firewall for external indicators.
- **The agent's outputs are your private security data.** Don't exfiltrate raw telemetry
  to external services; the agent summarizes findings to you. Only external indicator
  values leave the network, and only to the threat-intel providers you enabled. The IOC
  extractor drops RFC1918/internal addresses before any lookup.
- **The gateway holds the only SO write credential.** Nothing else can write to Security
  Onion. Keep `so.env`/`ti.env` at 0600 and out of version control (encrypt if versioned).
- **Headless runs carry Claude Code credentials.** The cycle/IR/self-improve runs
  authenticate via `claude.env` (an Anthropic API key). Keep it at 0600. Heavy fan-outs
  and over-frequent cycles cost real money and can hit rate limits; the suite runs a
  single daily cycle and on-demand IR for this reason.

## Defense in depth: how writes are controlled

| Layer | Mechanism |
|---|---|
| The cycle can't write | The daily cycle's `--allowedTools` excludes every write tool. It has no way to apply a tuning; it can only `propose_tuning` (read-only). |
| Single-use tokens | `propose_tuning` issues a one-time token + blast-radius preview. `apply_tuning` is the only write and consumes the token only on a successful PUT. Tokens are short word pairs (`amber-fox`) by design: a token is a workflow binding (it ties your approval to exactly the previewed change, once), not a security boundary. Any client that can reach the gateway can propose for itself, so the real boundary is network reachability plus the callers' tool allowlists. |
| Your approval | Applying requires your own act in Discord: `approve <token>` or a ✅ reaction on the proposal message. `disable`/`modify` get a second confirmation. |
| Audited + reversible | Every applied write is logged (SQLite audit DB) with the exact prior state, and is reversible via `revert_tuning`. |
| The IR team is read-only | The IR runner's allowlist is the read verbs only: no write/tune/disposition/Bash/Write. Two human gates bracket it (`investigate` to launch; you apply any recommended action yourself). |
| Self-improve is artifact-only | The worker's allowlist permits repo edits + commits to its own branch, never push/merge/live-apply/SSH/docker. Live-infra items become proposals. It checks spare capacity before starting and stops on a 429. |

The pattern throughout: tool-enforced restriction is the real boundary. The prompts only
restate the intent so the model never tries to route around the allowlist.

## Binding monitoring tenets

The agent's triage, tuning recommendations, and posture reports must conform to these
tenets. They exist because the natural-language shorthand they forbid ("trusted host",
"clean environment") is exactly how a real monitoring posture goes blind.

**1. No host is "trusted."** Suppression is narrow and behavior-specific, scoped to a
named, explained behavior pattern. Never host-wholesale, and never a license for a host to
become invisible. A suppressed alert is "explained," not "ignored": still recorded, still
counted toward the host's baseline. A host whitelist blinds exactly the asset an attacker
would target, so suppress the behavior (parent process + workdir + command shape), not the
host.

**2. The highest-privilege host gets the MOST scrutiny.** The asset that holds keys into
other boxes, runs broadly permissioned agentic tooling, and browses the web is the largest
attack surface and the prime target. It earns more monitoring, not less. Pair its
known-benign patterns with a behavioral baseline + deviation check, so a compromise that
mimics routine activity surfaces as a deviation instead of hiding inside a suppression.
"Most noise" must become "most baseline," not "most blind."

**3. Never report "clean." Report detections vs. coverage.** Absence of detection is not
absence of threat. Report what was detected vs. what the telemetry could see, within named
blind spots. The honest phrasing is "no adversarial activity detected in available
telemetry, bounded by coverage." Name the blind spots every time; state the effective
window and which sources were in scope.

**4. Epistemic humility + adversarial mindset.** Hold findings provisionally; reason like
an attacker. A null result may mean "nothing happened" OR "we could not see it";
distinguish the two and say which. Assume an adversary knows your blind spots
(VPN/tunneled egress, container east-west, DoH/DoT, same-segment unicast) and routes
around your sensors. Prefer "explained" over "benign," "not detected" over "absent,"
"bounded" over "complete."

These tenets are referenced by the `soc-analyst` skill, the cycle prompt, and the IR team
brief. They are normative, not advisory.

## Residual risks to accept consciously

- **The agent can be wrong.** It's a first-pass analyst. Verdicts are recommendations with
  evidence; you decide. Read the attached report, not just the briefing line.
- **A bad approval is on you.** The gate is only as good as the human at it. Reject
  proposals that look host-wholesale or under-evidenced; demand a narrower scope.
- **Cost exposure.** Heavy IR fan-outs and over-frequent cycles spend budget; the defaults
  (daily cycle, on-demand IR, self-improve off) are conservative on purpose.
- **Blind spots are real.** The agent reports them, but it cannot see what your sensors
  don't collect. Closing named blind spots is a recurring §3 recommendation, not a solved
  problem.
