"""Tuning orchestration: the gateway-enforced two-call approval gate.

This is the GATING SEAM (spec §4). It wires together the pure tuning logic
(``tuning.py``), the SO write client (``so_client.py``), and the audit/undo
store (``tuning_store.py``) into the four write tools:

    propose_tuning  -> validate + compute exact override + blast-radius +
                       issue a SINGLE-USE token. NO WRITE, fully injection-safe.
    apply_tuning    -> consume the token, capture prior state, PUT the change,
                       record the undo. The ONLY method that writes a tuning.
    revert_tuning   -> replay the captured prior state, mark reverted.
    list_tunings    -> currently-applied tunings + their undo handles.
    disposition_alerts -> acknowledge/escalate alerts (also audited).

SAFETY PROPERTIES enforced here (independent of the agent workflow):
  * No write without a valid, unused token from a prior ``propose`` (the seam
    the agent workflow's human-approval gate sits on top of).
  * Tokens are single-use -- a token cannot drive two writes.
  * Every applied write captures prior state BEFORE the PUT and is revertible.
  * propose is pure/read-only -- injected alert text can shape a proposal but
    cannot cause a write.

The human-approval gate itself (CC permission prompt / OpenClaw operator
affirmation) is layered ON TOP by the agent workflow; this service deliberately
does not auto-apply and exposes propose/apply as distinct calls so that gate has
a place to sit. ``disable``/``modify`` are flagged ``double_gated`` so the
workflow can demand a louder/second confirm.
"""

import uuid

from so_gateway import tuning
from so_gateway.so_client import SoClient
from so_gateway.tuning_store import TuningStore


class ProposalNotFoundError(KeyError):
    """apply/revert referenced a token/handle the gateway does not know."""


class TokenAlreadyUsedError(RuntimeError):
    """A single-use proposal token was presented a second time."""


class TuningService:
    def __init__(self, client: SoClient, store: TuningStore) -> None:
        self._client = client
        self._store = store
        # token -> pending proposal dict. In-memory: a single-use token only
        # has to outlive the propose->apply window of one approval. A gateway
        # restart invalidates pending proposals (fail-safe: re-propose).
        self._pending: dict[str, dict] = {}
        # Tokens whose apply is mid-PUT: guards a concurrent/retried apply
        # from driving a second write WHILE the first is in flight, without
        # permanently burning the token if that PUT then fails.
        self._in_flight: set[str] = set()
        # Tokens that were successfully applied -- tracked so a re-presented
        # token gives the precise "already used" error (single-use guarantee).
        self._consumed: set[str] = set()

    # -- propose -----------------------------------------------------------

    def propose_tuning(
        self,
        *,
        public_id: str,
        override_type: str,
        scope: dict,
        rationale: str,
        review_horizon_days: int | None = 90,
    ) -> dict:
        """Validate + preview a tuning and issue a single-use token. NO WRITE.

        Raises :class:`tuning.InvalidTuningError` on malformed input BEFORE any
        token is issued, so adversarial/injected alert content is rejected at
        the door and never produces a pending proposal.
        """
        # build_override validates type + scope; raises InvalidTuningError.
        override = tuning.build_override(override_type, scope, rationale)

        # Fetch the current detection (read-only) to compute the exact change
        # and capture the prior state for the eventual undo record.
        detection = self._client.get_detection_by_public_id(public_id)
        prior_state = tuning.capture_prior_state(detection)
        new_detection = tuning.apply_override(detection, override)

        blast_radius = self._estimate_blast_radius(public_id, override)

        token = uuid.uuid4().hex
        self._pending[token] = {
            "public_id": public_id,
            "detection_id": detection.get("id"),
            "override_type": override_type,
            "override": override,
            "prior_state": prior_state,
            "new_detection": new_detection,
            "rationale": rationale,
            "review_horizon_days": review_horizon_days,
        }

        return {
            "token": token,
            "public_id": public_id,
            "override_type": override_type,
            "override": override,
            "double_gated": override_type in tuning.DOUBLE_GATED_TYPES,
            "blast_radius": blast_radius,
            "review_horizon_days": review_horizon_days,
            # echo enough of the target for a human to recognise it
            "detection": {
                "id": detection.get("id"),
                "publicId": detection.get("publicId"),
                "title": detection.get("title"),
                "isEnabled": detection.get("isEnabled"),
                "current_override_count": len(detection.get("overrides") or []),
            },
        }

    def _estimate_blast_radius(self, public_id: str, override: dict) -> dict:
        """Best-effort estimate of how many recent alerts this would silence.

        Read-only. Failures degrade gracefully to ``{"available": False}`` so a
        blast-radius probe error never blocks a proposal (the human still
        approves; the count is advisory).
        """
        try:
            count = self._client.count_matching_alerts(public_id, override)
            return {"available": True, "matched_recent_alerts": count}
        except Exception as exc:  # noqa: BLE001 - advisory only
            return {"available": False, "error": str(exc)[:200]}

    # -- apply -------------------------------------------------------------

    def apply_tuning(self, token: str) -> dict:
        """Consume *token*, PUT the change, record the undo. Single-use."""
        if token in self._consumed:
            raise TokenAlreadyUsedError(
                "this proposal token was already applied (tokens are single-use)"
            )
        if token in self._in_flight:
            raise TokenAlreadyUsedError(
                "this proposal token is already being applied (apply in flight)"
            )
        if token not in self._pending:
            raise ProposalNotFoundError(
                "no pending proposal for this token (unknown or expired -- re-propose)"
            )
        proposal = self._pending[token]

        # Mark IN-FLIGHT (not yet consumed): a concurrent/retried apply of the
        # same token is rejected while the PUT runs, but a transient write
        # failure leaves the proposal re-appliable rather than burning the
        # token. The token is consumed only AFTER a successful PUT (F5).
        self._in_flight.add(token)
        try:
            self._client.put_detection(proposal["new_detection"])
        except Exception:
            # Write failed (e.g. SoWriteError after the re-auth retry): release
            # the in-flight lock so the SAME proposal can be re-applied once the
            # issue clears -- the token is NOT consumed; the proposal is intact.
            self._in_flight.discard(token)
            raise

        # PUT succeeded: the token is now spent.
        self._in_flight.discard(token)
        self._pending.pop(token, None)
        self._consumed.add(token)

        handle = self._store.record_apply(
            public_id=proposal["public_id"],
            detection_id=proposal["detection_id"] or "",
            override_type=proposal["override_type"],
            applied_override=proposal["override"],
            prior_state=proposal["prior_state"],
            rationale=proposal["rationale"],
            review_horizon_days=proposal["review_horizon_days"],
        )
        return {
            "handle": handle,
            "status": "applied",
            "public_id": proposal["public_id"],
            "override_type": proposal["override_type"],
        }

    # -- revert ------------------------------------------------------------

    def revert_tuning(self, handle: str) -> dict:
        """Replay the captured prior state for *handle* and mark it reverted."""
        rec = self._store.get(handle)
        if rec is None:
            raise ProposalNotFoundError(f"no tuning record for handle {handle!r}")
        if rec["status"] == "reverted":
            raise ValueError(f"tuning {handle!r} is already reverted")

        # Re-fetch current SO state so revert restores prior tuning onto the
        # live object (other SO-side fields stay current).
        current = self._client.get_detection_by_public_id(rec["public_id"])
        restored = tuning.revert_detection_state(current, rec["prior_state"])
        self._client.put_detection(restored)
        self._store.mark_reverted(handle)
        return {
            "handle": handle,
            "status": "reverted",
            "public_id": rec["public_id"],
        }

    # -- list --------------------------------------------------------------

    def list_tunings(self) -> list[dict]:
        """Currently-applied tunings + undo handles (excludes reverted)."""
        return self._store.list_applied()

    # -- disposition -------------------------------------------------------

    def disposition_alerts(
        self,
        *,
        rule_uuid: str,
        date_range: str,
        acknowledge: bool = True,
        escalate: bool = False,
    ) -> dict:
        """Acknowledge (close) / escalate alerts for a rule. Audited.

        Reversible by re-calling with ``acknowledge=False``. Recorded in the
        audit log with ``override_type='disposition'`` so the trail is complete.
        """
        result = self._client.disposition_alerts(
            rule_uuid=rule_uuid,
            date_range=date_range,
            acknowledge=acknowledge,
            escalate=escalate,
        )
        handle = self._store.record_apply(
            public_id=rule_uuid,
            detection_id="",
            override_type="disposition",
            applied_override={
                "acknowledge": acknowledge,
                "escalate": escalate,
                "date_range": date_range,
            },
            prior_state={"isEnabled": True, "overrides": []},
            rationale=f"disposition acknowledge={acknowledge} escalate={escalate}",
            review_horizon_days=None,
        )
        return {"handle": handle, "status": "dispositioned", "result": result}
