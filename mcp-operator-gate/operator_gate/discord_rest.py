"""A tiny Discord REST client — post, edit, react, read reactions, read replies.

Deliberately REST-only: no gateway/WebSocket connection. That is the whole point
of this service. OpenClaw already holds the bot's single gateway connection, so a
second connection on the same token would conflict, and an HTTP interactions
endpoint is application-global (it would divert OpenClaw's own slash commands and
needs inbound exposure this deployment doesn't have). Polling reactions over REST
sidesteps all of that: same bot token, no gateway, no conflict, works today.
"""

from __future__ import annotations

import time
import urllib.parse

import httpx

API = "https://discord.com/api/v10"


class DiscordREST:
    def __init__(self, token: str, *, timeout: float = 15.0) -> None:
        self._client = httpx.Client(
            base_url=API,
            headers={
                "Authorization": f"Bot {token}",
                "User-Agent": "DiscordBot (onionclaw-operator-gate, 1.0)",
            },
            timeout=timeout,
        )

    # -- one polite 429-aware request -----------------------------------------
    def _req(self, method: str, path: str, **kw) -> httpx.Response:
        for _ in range(4):
            r = self._client.request(method, path, **kw)
            if r.status_code == 429:  # rate limited: honor retry_after and retry
                retry = r.json().get("retry_after", 1.0)
                time.sleep(min(float(retry) + 0.1, 5.0))
                continue
            r.raise_for_status()
            return r
        r.raise_for_status()
        return r

    # -- messages -------------------------------------------------------------
    def post_message(self, channel_id: str, content: str, *, reply_to: str | None = None) -> str:
        body: dict = {"content": content}
        if reply_to:
            # A real Discord reply: threads under the target and notifies. The
            # fail_if_not_exists=False keeps it a plain post if the target is gone.
            body["message_reference"] = {"message_id": reply_to, "fail_if_not_exists": False}
        r = self._req("POST", f"/channels/{channel_id}/messages", json=body)
        return r.json()["id"]

    def edit_message(self, channel_id: str, message_id: str, content: str) -> None:
        self._req(
            "PATCH", f"/channels/{channel_id}/messages/{message_id}",
            json={"content": content},
        )

    def get_message(self, channel_id: str, message_id: str) -> dict:
        return self._req("GET", f"/channels/{channel_id}/messages/{message_id}").json()

    def list_messages(self, channel_id: str, *, limit: int = 50, after: str | None = None) -> list[dict]:
        params = {"limit": limit}
        if after:
            params["after"] = after
        return self._req("GET", f"/channels/{channel_id}/messages", params=params).json()

    # -- reactions ------------------------------------------------------------
    def add_reaction(self, channel_id: str, message_id: str, emoji: str) -> None:
        e = urllib.parse.quote(emoji)
        self._req(
            "PUT",
            f"/channels/{channel_id}/messages/{message_id}/reactions/{e}/@me",
        )

    def reaction_users(self, channel_id: str, message_id: str, emoji: str) -> list[dict]:
        e = urllib.parse.quote(emoji)
        return self._req(
            "GET", f"/channels/{channel_id}/messages/{message_id}/reactions/{e}"
        ).json()

    def me(self) -> dict:
        return self._req("GET", "/users/@me").json()

    def close(self) -> None:
        self._client.close()
