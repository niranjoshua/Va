"""WhatsApp webhooks: delivery receipts, replies and opt-outs.

Meta calls our webhook when a message is sent, delivered, read or fails, and
when someone replies. We use that to:
  - track each plan to "read" (did the site manager see it?);
  - record whether the plan was followed (reply 1) or not (reply 2), which is
    how adoption is measured;
  - honour STOP at once, and START to resume.

Every POST is checked against the X-Hub-Signature-256 header with the app
secret, so nobody else can forge receipts or opt people out. Providers retry
webhooks, so replies are stored once by message id.
"""

from __future__ import annotations

import hashlib
import hmac
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from vaticore.delivery.recipients import recipient_hash
from vaticore.pipeline.store import PlanStore

_FOLLOWED = {"1", "yes", "y", "followed", "done", "ok", "followed plan"}
_NOT_FOLLOWED = {"2", "no", "n", "not followed", "did not follow"}
_STOP = {"stop", "unsubscribe", "cancel", "end", "quit", "stop all"}
_START = {"start", "subscribe", "resume", "unstop"}


def verify_signature(app_secret: str, body: bytes, header: str | None) -> bool:
    """True if the X-Hub-Signature-256 header matches the raw request body."""
    if not header or not header.startswith("sha256="):
        return False
    expected = hmac.new(app_secret.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, header.removeprefix("sha256="))


def classify_reply(text: str) -> str:
    """followed, not_followed, stop, start or other."""
    cleaned = " ".join(text.strip().lower().replace(".", " ").replace("!", " ").split())
    if cleaned in _STOP:
        return "stop"
    if cleaned in _START:
        return "start"
    if cleaned in _FOLLOWED:
        return "followed"
    if cleaned in _NOT_FOLLOWED:
        return "not_followed"
    return "other"


@dataclass(frozen=True)
class WebhookOutcome:
    statuses: int = 0
    replies: int = 0
    opt_outs: int = 0
    opt_ins: int = 0


def handle_webhook(store: PlanStore, payload: dict[str, Any]) -> WebhookOutcome:
    """Apply one webhook payload to the store."""
    statuses = replies = opt_outs = opt_ins = 0
    for entry in payload.get("entry", []):
        for change in entry.get("changes", []):
            value = change.get("value", {})
            for status in value.get("statuses", []):
                errors = status.get("errors") or []
                error = errors[0].get("title") if errors else None
                if store.update_delivery_status(
                    str(status.get("id")),
                    str(status.get("status")),
                    _when(status.get("timestamp")),
                    error,
                ):
                    statuses += 1
            for message in value.get("messages", []):
                text = _message_text(message)
                sender = "+" + str(message.get("from", "")).lstrip("+")
                try:
                    who = recipient_hash(sender)
                except ValueError:
                    continue
                kind = classify_reply(text)
                at = _when(message.get("timestamp"))
                if kind == "stop":
                    store.set_opt_out(who, True, "whatsapp reply", at)
                    opt_outs += 1
                elif kind == "start":
                    store.set_opt_out(who, False, "whatsapp reply", at)
                    opt_ins += 1
                if store.record_feedback(
                    provider_message_id=str(message.get("id")),
                    received_at=at,
                    recipient_hash=who,
                    kind=kind,
                    text=text[:500],
                    plan=store.last_delivery_to(who),
                ):
                    replies += 1
    return WebhookOutcome(statuses, replies, opt_outs, opt_ins)


def _message_text(message: dict[str, Any]) -> str:
    kind = message.get("type")
    if kind == "text":
        return str(message.get("text", {}).get("body", ""))
    if kind == "button":  # a quick reply button on a template
        button = message.get("button", {})
        return str(button.get("payload") or button.get("text") or "")
    if kind == "interactive":
        reply = message.get("interactive", {})
        chosen = reply.get("button_reply") or reply.get("list_reply") or {}
        return str(chosen.get("id") or chosen.get("title") or "")
    return ""


def _when(epoch: Any) -> datetime:
    try:
        return datetime.fromtimestamp(int(epoch), tz=UTC)
    except (TypeError, ValueError):
        return datetime.now(tz=UTC)
