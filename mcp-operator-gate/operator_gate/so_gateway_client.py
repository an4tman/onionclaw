"""Minimal streamable-HTTP MCP client for the so-gateway (:9221).

The async tuning-approval watcher uses this to call ``apply_tuning`` /
``list_pending_proposals`` when the operator taps ✅ — the gateway's single-use
token gate still applies, so this client is just the operator's tap made into
the one write the cycle deliberately could not make itself.
"""

from __future__ import annotations

import json

import httpx


class SoGatewayError(RuntimeError):
    pass


class SoGatewayClient:
    def __init__(self, url: str, *, timeout: float = 60.0) -> None:
        self._url = url
        self._timeout = timeout
        self._client = httpx.Client(timeout=timeout)
        self._session: str | None = None
        self._id = 0

    def _headers(self) -> dict:
        h = {"Content-Type": "application/json",
             "Accept": "application/json, text/event-stream"}
        if self._session:
            h["mcp-session-id"] = self._session
        return h

    def _rpc(self, method: str, params: dict | None = None, *, notify: bool = False):
        body = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            body["params"] = params
        if not notify:
            self._id += 1
            body["id"] = self._id
        r = self._client.post(self._url, headers=self._headers(), content=json.dumps(body))
        if not self._session:
            self._session = r.headers.get("mcp-session-id")
        r.raise_for_status()
        if notify:
            return None
        for line in r.text.splitlines():
            if line.startswith("data:"):
                d = json.loads(line[5:])
                if "error" in d:
                    raise SoGatewayError(str(d["error"]))
                return d.get("result")
        raise SoGatewayError("no result frame in MCP response")

    def _ensure_session(self) -> None:
        if self._session:
            return
        self._rpc("initialize", {
            "protocolVersion": "2024-11-05", "capabilities": {},
            "clientInfo": {"name": "operator-gate", "version": "1"},
        })
        self._rpc("notifications/initialized", {}, notify=True)

    def call_tool(self, name: str, arguments: dict):
        self._ensure_session()
        res = self._rpc("tools/call", {"name": name, "arguments": arguments})
        if res.get("isError"):
            text = (res.get("content") or [{}])[0].get("text", "tool error")
            raise SoGatewayError(text)
        sc = res.get("structuredContent")
        if sc is not None:
            return sc.get("result", sc)
        content = res.get("content") or []
        if content and content[0].get("type") == "text":
            try:
                return json.loads(content[0]["text"])
            except json.JSONDecodeError:
                return content[0]["text"]
        return None

    def apply_tuning(self, token: str) -> dict:
        return self.call_tool("apply_tuning", {"token": token})
