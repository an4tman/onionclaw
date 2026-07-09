# mcp-operator-gate

The **general human-in-the-loop gate** for the OnionClaw SOC suite. It gives every
agent one primitive — *ask the operator a question and get a tapped answer* — and
gives the headless daily cycle a *tap-✅-to-approve* path for its tuning proposals.
Both are **reaction-based over the Discord REST API**, so they work on the same bot
token OpenClaw already uses, with **no second gateway connection and no inbound
exposure**.

## Why reactions, not native Discord buttons

Discord buttons render fine (verified). But a button **click** is delivered as an
`INTERACTION_CREATE` event over the bot's **gateway connection** — which OpenClaw
holds. On a shared token a second gateway connection conflicts, and registering an
HTTP interactions endpoint is application-global (it would divert OpenClaw's own
slash commands and needs an inbound HTTPS endpoint this deployment doesn't expose).
Polling **reactions** over REST needs no gateway at all: same token, no conflict,
works today. If a future OpenClaw build delivers reaction/interaction events to
agents natively, this service can be retired in favor of that.

## What it provides

- **`ask_operator(question, options, timeout_seconds?)`** — an MCP tool. Any agent
  posts a one-line question with 1–10 short option labels; the operator taps one
  emoji; the agent gets `{answered, option, index, cancelled, timed_out}`. Use it
  for approvals, escalation go/no-go, "which of these?", or any subjective call.
  Only the configured operator's reaction counts.
- **Tuning-approval watcher** — a background loop that seeds ✅/❌ on every cycle
  `PROPOSAL —` message and, when the operator taps ✅, performs the single gated
  `apply_tuning` write directly (via the so-gateway) and edits the message with the
  outcome + undo handle (❌ dismisses). Stateless: it recognizes already-handled
  messages by the marker it writes, so a restart never double-applies.

## Deploy

```bash
cp operator-gate.env.example operator-gate.env   # then fill it in (chmod 600)
./deploy.sh
```

`operator-gate.env`:

```ini
DISCORD_TOKEN=<same bot token OpenClaw uses>
OPERATOR_CHANNEL_ID=<the SOC/operator channel id>
OPERATOR_USER_ID=<your Discord user id — only your reactions count>
SO_GATEWAY_URL=http://mcp-so-gateway:8080/mcp
# WATCH_APPROVALS=false   # to run ask_operator only, no tuning watcher
```

Then wire `operator_gate` into OpenClaw's `mcp.servers` and (for headless use) as a
user-scoped Claude Code MCP, exactly like the other gateways
(`http://<host>:9225/mcp`).

## Test

```bash
pip install -e '.[dev]' && pytest -q
```
