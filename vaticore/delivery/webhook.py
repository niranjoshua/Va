"""WhatsApp webhooks: delivery receipts, replies and opt-outs.

Meta calls our webhook when a message is sent, delivered, read or fails, and
when someone replies. We use that to:
  - track each plan to "read" (did the site manager see it?);
  - record whether the plan was followed (reply 1) or not (reply 2), which is
    how adoption is measured;
  - after a 2, ask why (generator fault, no diesel, grid came on...) and store
    the answer, so a plan that could not be carried out is told apart from a
    plan that was wrong;
  - honour STOP at once, and START to resume.

The question after a 2 is free text, which WhatsApp allows because the person
has just written to us (the 24-hour window is open). It is asked once per plan
day; the answer is read as a reason only if it comes within REASON_WINDOW.

Every POST is checked against the X-Hub-Signature-256 header with the app
secret, so nobody else can forge receipts or opt people out. Providers retry
webhooks, so replies are stored once by message id.
"""

from __future__ import annotations

import hashlib
import hmac
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import pandas as pd

from vaticore.delivery.recipients import recipient_hash
from vaticore.pipeline.store import PlanStore

_FOLLOWED = {"1", "yes", "y", "followed", "done", "ok", "followed plan"}
_NOT_FOLLOWED = {"2", "no", "n", "not followed", "did not follow"}
_STOP = {"stop", "unsubscribe", "cancel", "end", "quit", "stop all"}
_START = {"start", "subscribe", "resume", "unstop"}

# Why a plan was not followed. Letters, so they never clash with replies 1 and 2.
REASONS: dict[str, tuple[str, str]] = {
    "a": ("generator_fault", "generator fault"),
    "b": ("no_diesel", "no diesel"),
    "c": ("grid_on", "grid was on"),
    "d": ("battery_problem", "battery problem"),
    "e": ("instructed", "told to run it differently"),
}
REASON_LABELS = {code: label for code, label in REASONS.values()} | {"other": "other reason"}
_REASON_WORDS = (
    ("generator_fault", ("fault", "faulty", "broke", "broken", "repair", "spoil", "spoilt")),
    ("no_diesel", ("diesel", "fuel")),
    ("grid_on", ("grid", "nepa", "phcn", "light came", "light dey", "bring light", "power came")),
    ("battery_problem", ("battery", "batteries", "inverter")),
    ("instructed", ("told", "instruct", "manager said", "oga", "dem tell")),
)
REASON_QUESTION = (
    "Thanks. Why was the plan not followed? Reply A generator fault, B no diesel, "
    "C grid was on, D battery problem, E told to run it differently, or type the reason."
)
REASON_THANKS = "Thank you, noted. It helps make tomorrow's plan better."
# The question and thanks in each plan language (English above).
REASON_QUESTIONS = {
    "en": REASON_QUESTION,
    "pcm": (
        "Thank you. Why una no follow the plan? Reply A generator spoil, B no diesel, "
        "C grid (NEPA) bring light, D battery wahala, E dem tell una make una run am "
        "another way, or type the reason."
    ),
}
REASON_THANKS_BY_LANGUAGE = {
    "en": REASON_THANKS,
    "pcm": "Thank you, we don hear. E go help make tomorrow plan better.",
}
REASON_WINDOW = timedelta(hours=24)

# Sends a free-text WhatsApp message (phone, text); None where sending is not set up.
TextSender = Callable[[str, str], Any]


def verify_signature(app_secret: str, body: bytes, header: str | None) -> bool:
    """True if the X-Hub-Signature-256 header matches the raw request body."""
    if not header or not header.startswith("sha256="):
        return False
    expected = hmac.new(app_secret.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, header.removeprefix("sha256="))


def classify_reason(text: str) -> str:
    """A reason code for why a plan was not followed; 'other' for free text."""
    cleaned = " ".join(text.strip().lower().replace(".", " ").replace(")", " ").split())
    if cleaned[:1] in REASONS and (len(cleaned) == 1 or cleaned[1] == " "):
        return REASONS[cleaned[:1]][0]
    for code, words in _REASON_WORDS:
        if any(word in cleaned for word in words):
            return code
    return "other"


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
    reasons: int = 0
    questions: int = 0


def handle_webhook(
    store: PlanStore, payload: dict[str, Any], *, send_text: TextSender | None = None
) -> WebhookOutcome:
    """Apply one webhook payload to the store.

    send_text, when given, is used to ask why after a reply of 2, and to thank
    the person for the answer.
    """
    statuses = replies = opt_outs = opt_ins = reasons = questions = 0
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
                plan = store.last_delivery_to(who)
                reason = None
                previous = store.last_feedback_from(who)
                if kind == "other" and _awaiting_reason(previous, at):
                    assert previous is not None
                    kind, reason = "reason", classify_reason(text)
                    plan = (
                        str(previous["operator_id"]),
                        str(previous["site_id"]),
                        pd.Timestamp(previous["plan_date"]).date(),
                    )
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
                    plan=plan,
                    reason=reason,
                ):
                    replies += 1
                    language = store.last_delivery_language(who)
                    if kind == "reason":
                        reasons += 1
                        thanks = REASON_THANKS_BY_LANGUAGE.get(language, REASON_THANKS)
                        _send(send_text, sender, thanks)
                    elif kind == "not_followed" and plan is not None:
                        question = REASON_QUESTIONS.get(language, REASON_QUESTION)
                        if _send(send_text, sender, question):
                            questions += 1
    return WebhookOutcome(statuses, replies, opt_outs, opt_ins, reasons, questions)


def _awaiting_reason(previous: dict[str, Any] | None, at: datetime) -> bool:
    """True if the person's last reply was a 2 for a plan, recently enough."""
    if previous is None or previous.get("kind") != "not_followed":
        return False
    if previous.get("operator_id") is None or pd.isna(previous.get("plan_date")):
        return False
    received = pd.Timestamp(previous["received_at"])
    received = received.tz_localize("UTC") if received.tzinfo is None else received
    return bool(pd.Timestamp(at) - received <= REASON_WINDOW)


def _send(send_text: TextSender | None, to: str, text: str) -> bool:
    if send_text is None:
        return False
    try:
        send_text(to, text)
    except Exception:  # a failed courtesy message must never lose the reply
        return False
    return True


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
