"""Delivery channels: WhatsApp (Meta's Cloud API) and a console dry run.

WhatsApp rules that shape this code:
  - A business may message someone first only with a pre-approved template.
    Daily plans therefore use the "vaticore_daily_plan" utility template
    (docs/whatsapp-setup.md), with the plan in its six variables.
  - Free text is allowed only within 24 hours of the person's last message to
    us. mode="text" is for that, and for testing.
  - Only people who agreed to receive messages may be sent them (consent is
    enforced in recipients.py).

Sending is retried with backoff on rate limits, server errors and network
failures, never on a rejected request (a wrong number or template), and every
attempt's outcome is returned so it can be stored.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Protocol

import httpx

from vaticore.delivery.message import PlanMessage
from vaticore.delivery.recipients import mask_phone, normalise_phone

GRAPH_URL = "https://graph.facebook.com"
# WhatsApp error codes that mean "slow down" or "try again later".
_RETRY_CODES = {4, 80007, 130429, 131000, 131016, 133004}


@dataclass(frozen=True)
class SendResult:
    status: str  # sent, failed or dry_run
    provider_message_id: str | None = None
    error: str | None = None
    attempts: int = 1


class Channel(Protocol):
    name: str

    def send(self, to: str, message: PlanMessage) -> SendResult: ...


class ConsoleChannel:
    """Print messages instead of sending them: for demos, dry runs and tests."""

    name = "console"

    def __init__(self, printer: Callable[[str], None] = print) -> None:
        self._print = printer

    def send(self, to: str, message: PlanMessage) -> SendResult:
        self._print(f"--- to {mask_phone(to)} ---\n{message.text}\n")
        return SendResult(status="dry_run")


class WhatsAppChannel:
    """WhatsApp Business Cloud API (graph.facebook.com)."""

    name = "whatsapp"

    def __init__(
        self,
        *,
        token: str,
        phone_number_id: str,
        api_version: str = "v23.0",
        template_name: str = "vaticore_daily_plan",
        template_language: str = "en",
        mode: str = "template",
        max_attempts: int = 3,
        client: httpx.Client | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        if mode not in ("template", "text"):
            raise ValueError("mode must be 'template' or 'text'")
        if not token or not phone_number_id:
            raise ValueError("WhatsApp needs an access token and a phone number ID")
        self._url = f"{GRAPH_URL}/{api_version}/{phone_number_id}/messages"
        self._headers = {"Authorization": f"Bearer {token}"}
        self._template = template_name
        self._language = template_language
        self._mode = mode
        self._max_attempts = max_attempts
        self._client = client or httpx.Client(timeout=20.0)
        self._sleep = sleep

    def send(self, to: str, message: PlanMessage) -> SendResult:
        if self._mode == "text":
            body: dict[str, Any] = {
                "type": "text",
                "text": {"preview_url": False, "body": message.text},
            }
        else:
            body = {
                "type": "template",
                "template": {
                    "name": self._template,
                    "language": {"code": self._language},
                    "components": [
                        {
                            "type": "body",
                            "parameters": [{"type": "text", "text": p} for p in message.params],
                        }
                    ],
                },
            }
        return self._post(to, body)

    def send_hello_world(self, to: str) -> SendResult:
        """Meta's pre-approved sample template: checks the credentials work."""
        return self._post(
            to,
            {
                "type": "template",
                "template": {"name": "hello_world", "language": {"code": "en_US"}},
            },
        )

    def _post(self, to: str, body: dict[str, Any]) -> SendResult:
        payload = {
            "messaging_product": "whatsapp",
            "recipient_type": "individual",
            "to": normalise_phone(to).lstrip("+"),
            **body,
        }
        error = "not sent"
        for attempt in range(1, self._max_attempts + 1):
            try:
                response = self._client.post(self._url, headers=self._headers, json=payload)
            except httpx.HTTPError as exc:
                error = f"network error: {exc}"
                retry = True
            else:
                if response.status_code < 300:
                    data = response.json()
                    message_id = (data.get("messages") or [{}])[0].get("id")
                    return SendResult("sent", message_id, None, attempt)
                error, retry = _describe_error(response)
            if not retry or attempt == self._max_attempts:
                return SendResult("failed", None, error, attempt)
            self._sleep(2.0 ** (attempt - 1))
        return SendResult("failed", None, error, self._max_attempts)


def _describe_error(response: httpx.Response) -> tuple[str, bool]:
    """A readable error and whether it is worth retrying."""
    try:
        detail = response.json().get("error", {})
    except ValueError:
        detail = {}
    code = detail.get("code")
    text = detail.get("message") or response.text[:200]
    retry = response.status_code == 429 or response.status_code >= 500 or code in _RETRY_CODES
    hint = ""
    if code == 131047:
        hint = " (more than 24 h since this person last replied: use the template mode)"
    elif code == 132001:
        hint = " (template not found or not approved for this language)"
    elif code in (131026, 131030):
        hint = " (number not on WhatsApp or not allowed for this test number)"
    elif response.status_code in (401, 403) or code == 190:
        hint = " (access token invalid or expired)"
    return f"HTTP {response.status_code}, code {code}: {text}{hint}", retry
