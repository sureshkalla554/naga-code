"""Approve actions from your phone while Naga runs unattended (background jobs, schedules, `naga -p`).

Uses the ntfy protocol (https://ntfy.sh, or a self-hosted ntfy server): Naga publishes the request to a secret
topic with Approve / Deny buttons; tapping a button publishes "<decision> <nonce>" to the topic's reply channel,
which Naga is polling. No inbound connection to this PC is needed. Off unless `notify.ntfy_topic` is set.

Safety: a random per-request nonce ties each reply to one request; anything else on the reply topic is ignored.
No answer before the timeout means deny. Dangerous actions are never approvable remotely.
"""

from __future__ import annotations

import asyncio
import json
import secrets
import time
from typing import Any

import httpx

from .security.commands import Risk
from .security.permissions import ApprovalRequest, ApprovalResponse, Choice


def new_topic() -> str:
    return "naga-" + secrets.token_urlsafe(18).replace("_", "").replace("-", "")[:24]


class Notifier:
    def __init__(self, server: str, topic: str, token: str | None = None, timeout_s: float = 600.0):
        self.server = server.rstrip("/")
        self.topic = topic
        self.timeout_s = timeout_s
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        self._client = httpx.AsyncClient(timeout=30.0, headers=headers)

    @classmethod
    def from_config(cls, config) -> "Notifier | None":
        n = config.section("notify") if hasattr(config, "section") else {}
        if not n.get("ntfy_topic"):
            return None
        return cls(n.get("ntfy_server", "https://ntfy.sh"), n["ntfy_topic"], n.get("ntfy_token"),
                   float(n.get("approval_timeout_s", 600)))

    async def publish(self, title: str, message: str, *, actions: list[dict] | None = None, topic: str | None = None,
                      priority: int = 3, tags: list[str] | None = None) -> None:
        body: dict[str, Any] = {"topic": topic or self.topic, "title": title[:250], "message": message[:3500],
                                "priority": priority}
        if actions:
            body["actions"] = actions
        if tags:
            body["tags"] = tags
        r = await self._client.post(self.server, json=body)
        r.raise_for_status()

    async def wait_reply(self, nonce: str, since: int) -> str | None:
        """Poll the reply topic until 'approve|deny <nonce>' arrives; None on timeout."""
        deadline = time.monotonic() + self.timeout_s
        url = f"{self.server}/{self.topic}-reply/json"
        while time.monotonic() < deadline:
            try:
                r = await self._client.get(url, params={"poll": "1", "since": str(since)})
                for line in r.text.splitlines():
                    try:
                        msg = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    parts = str(msg.get("message", "")).split()
                    if len(parts) == 2 and parts[1] == nonce and parts[0] in ("approve", "deny"):
                        return parts[0]
            except httpx.HTTPError:
                pass
            await asyncio.sleep(3)
        return None

    async def aclose(self) -> None:
        await self._client.aclose()


class PhoneApprover:
    """An ApprovalHandler that asks on your phone instead of the terminal."""

    def __init__(self, notifier: Notifier, label: str = "Naga"):
        self.notifier = notifier
        self.label = label

    async def __call__(self, req: ApprovalRequest) -> ApprovalResponse:
        if req.level >= Risk.DANGEROUS:
            return ApprovalResponse(Choice.DENY, note="dangerous actions can only be approved at the computer")
        nonce = secrets.token_hex(8)
        since = int(time.time()) - 1
        reply = f"{self.notifier.server}/{self.notifier.topic}-reply"
        details = [f"{req.tool}: {req.operation}", f"in {req.cwd}" if req.cwd else "",
                   f"why: {req.reason}" if req.reason else "", f"risk: {req.level.name.lower()}; {req.impact}"]
        try:
            await self.notifier.publish(
                f"{self.label} needs approval", "\n".join(d for d in details if d), priority=4, tags=["warning"],
                actions=[{"action": "http", "label": "Approve", "url": reply, "method": "POST",
                          "body": f"approve {nonce}", "clear": True},
                         {"action": "http", "label": "Deny", "url": reply, "method": "POST",
                          "body": f"deny {nonce}", "clear": True}])
        except httpx.HTTPError as e:
            return ApprovalResponse(Choice.DENY, note=f"could not reach the notification server: {e}")
        decision = await self.notifier.wait_reply(nonce, since)
        if decision == "approve":
            return ApprovalResponse(Choice.APPROVE_ONCE)
        return ApprovalResponse(Choice.DENY, note="denied on phone" if decision else "no answer from phone in time")
