"""Messages, recipients, the WhatsApp channel and webhooks."""

from __future__ import annotations

import hashlib
import hmac
import json
from datetime import date, datetime

import httpx
import pandas as pd
import pytest

from vaticore.delivery.channels import ConsoleChannel, WhatsAppChannel
from vaticore.delivery.message import (
    TEMPLATE_BODY,
    PlanMessage,
    day_label,
    join_spans,
    plan_message,
    run_windows,
)
from vaticore.delivery.recipients import (
    load_recipients,
    mask_phone,
    normalise_phone,
    recipient_hash,
    recipients_for,
)
from vaticore.delivery.webhook import classify_reply, handle_webhook, verify_signature
from vaticore.pipeline.store import PlanStore

LAGOS = "Africa/Lagos"
START = pd.Timestamp("2026-03-10 05:00", tz="UTC")  # 06:00 in Lagos
HOURS = pd.date_range(START, periods=24, freq="h")


def _message() -> PlanMessage:
    return PlanMessage("Tower One", "Tue 10 Mar", "18:00 to 20:00.", "none.", "ends 60%.", "OK")


# -- message -----------------------------------------------------------------


def test_day_label_and_windows_use_the_site_clock() -> None:
    assert day_label(START, START + pd.Timedelta(hours=24), LAGOS) == (
        "Tue 10 Mar, 06:00 to 06:00 Wed"
    )
    on = [False] * 24
    for i in (13, 14, 18, 19, 20):  # 19:00, 20:00 and 00:00 to 03:00 local
        on[i] = True
    assert run_windows(HOURS, on, LAGOS) == ["19:00 to 21:00", "00:00 to 03:00"]
    on = [False] * 17 + [True] * 7  # 23:00 to 06:00 local
    assert run_windows(HOURS, on, LAGOS) == ["23:00 to 06:00"]


def test_long_lists_of_runs_stay_readable() -> None:
    assert join_spans(["a"]) == "a"
    assert join_spans(["a", "b", "c"]) == "a, b and c"
    assert join_spans([str(i) for i in range(7)]) == "0, 1, 2, 3 and 3 more"


def test_plan_message_reads_like_an_instruction() -> None:
    on = [False] * 24
    on[13:15] = [True, True]  # 19:00 to 21:00 local
    message = plan_message(
        site_name="Tower One",
        timezone=LAGOS,
        start=START,
        end=START + pd.Timedelta(hours=24),
        timestamps=HOURS,
        genset_on=on,
        grid_on=[True] * 8 + [False] * 16,
        has_generator=True,
        fuel_l=7.4,
        genset_hours=2.0,
        unserved_kwh=0.0,
        soc_start_pct=60.0,
        soc_end_pct=41.0,
        soc_assumed=True,
        data_note="Data OK.",
        track_note="Last 7 days: the forecast range held 81% of hours.",
    )
    assert message.generator == "19:00 to 21:00 (2 h in 1 run, about 7 L)."
    assert message.grid == "counted on 06:00 to 14:00; run the generator if it fails."
    assert message.battery == "starts about 60% (assumed), ends about 41%."
    text = message.text
    assert text.startswith("Vaticore plan for Tower One, Tue 10 Mar, 06:00 to 06:00 Wed.")
    assert "Advisory only" in text and "STOP" in text
    assert "—" not in text  # house style: no em dashes


def test_template_variables_are_single_lines_within_limits() -> None:
    message = PlanMessage("A\nB", "x", "y" * 500, "z\t z", "w    w", "v")
    params = message.params
    assert params[0] == "A B" and params[3] == "z z" and params[4] == "w w"
    assert len(params[2]) == 200 and params[2].endswith("...")
    assert TEMPLATE_BODY.count("{{") == 6
    assert not TEMPLATE_BODY.startswith("{{") and not TEMPLATE_BODY.rstrip().endswith("}}")


# -- recipients ----------------------------------------------------------------


@pytest.mark.parametrize(
    "raw", ["+234 800 000 0001", "002348000000001", "2348000000001", "+234-800-000-0001"]
)
def test_phone_numbers_are_normalised(raw: str) -> None:
    assert normalise_phone(raw) == "+2348000000001"


def test_bad_numbers_and_private_forms() -> None:
    with pytest.raises(ValueError):
        normalise_phone("08000000001")  # local format: country code needed
    assert mask_phone("+2348012345678") == "+234******5678"
    digest = recipient_hash("+234 801 234 5678")
    assert digest == recipient_hash("2348012345678") and "2348012345678" not in digest


def test_only_consenting_recipients_for_the_site(tmp_path: object) -> None:
    from pathlib import Path

    path = Path(str(tmp_path)) / "recipients.toml"
    path.write_text(
        """
[[recipient]]
name = "A"
whatsapp = "+2348000000001"
operator_id = "op"
sites = ["s1"]
consent = true

[[recipient]]
name = "B"
whatsapp = "+2348000000002"
operator_id = "op"
sites = ["*"]
consent = true

[[recipient]]
name = "C"
whatsapp = "+2348000000003"
operator_id = "op"
sites = ["s1"]
consent = false
"""
    )
    people = load_recipients(path)
    assert [p.name for p in recipients_for(people, "op", "s1")] == ["A", "B"]
    assert [p.name for p in recipients_for(people, "op", "s2")] == ["B"]
    assert recipients_for(people, "other", "s1") == []


def test_the_example_recipients_file_loads() -> None:
    people = load_recipients("examples/sites/recipients.example.toml")
    assert len(people) == 3 and not people[2].consent


# -- WhatsApp channel ------------------------------------------------------------


def _channel(handler: object, **kwargs: object) -> tuple[WhatsAppChannel, list[float]]:
    sleeps: list[float] = []
    client = httpx.Client(transport=httpx.MockTransport(handler))  # type: ignore[arg-type]
    channel = WhatsAppChannel(
        token="TOKEN",
        phone_number_id="123456",
        client=client,
        sleep=sleeps.append,
        **kwargs,  # type: ignore[arg-type]
    )
    return channel, sleeps


def test_a_plan_goes_out_as_the_approved_template() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"messages": [{"id": "wamid.ABC"}]})

    channel, _ = _channel(handler)
    result = channel.send("+234 800 000 0001", _message())
    assert result.status == "sent" and result.provider_message_id == "wamid.ABC"

    request = seen[0]
    assert str(request.url) == "https://graph.facebook.com/v23.0/123456/messages"
    assert request.headers["authorization"] == "Bearer TOKEN"
    body = json.loads(request.content)
    assert body["to"] == "2348000000001" and body["messaging_product"] == "whatsapp"
    template = body["template"]
    assert template["name"] == "vaticore_daily_plan" and template["language"] == {"code": "en"}
    params = template["components"][0]["parameters"]
    assert [p["text"] for p in params] == _message().params


def test_text_mode_sends_the_whole_message() -> None:
    bodies: list[dict[str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        bodies.append(json.loads(request.content))
        return httpx.Response(200, json={"messages": [{"id": "wamid.T"}]})

    channel, _ = _channel(handler, mode="text")
    channel.send("+2348000000001", _message())
    assert bodies[0]["type"] == "text"
    assert bodies[0]["text"] == {"preview_url": False, "body": _message().text}


def test_rate_limits_are_retried_with_backoff() -> None:
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] < 3:
            return httpx.Response(429, json={"error": {"code": 130429, "message": "slow down"}})
        return httpx.Response(200, json={"messages": [{"id": "wamid.R"}]})

    channel, sleeps = _channel(handler)
    result = channel.send("+2348000000001", _message())
    assert result.status == "sent" and result.attempts == 3 and sleeps == [1.0, 2.0]


def test_rejected_requests_fail_at_once_with_a_hint() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            400, json={"error": {"code": 132001, "message": "Template name does not exist"}}
        )

    channel, sleeps = _channel(handler)
    result = channel.send("+2348000000001", _message())
    assert result.status == "failed" and result.attempts == 1 and sleeps == []
    assert "not approved" in (result.error or "")


def test_network_errors_are_retried_then_reported() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("no route")

    channel, sleeps = _channel(handler)
    result = channel.send("+2348000000001", _message())
    assert result.status == "failed" and result.attempts == 3 and len(sleeps) == 2
    assert "network error" in (result.error or "")


def test_hello_world_checks_the_credentials() -> None:
    bodies: list[dict[str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        bodies.append(json.loads(request.content))
        return httpx.Response(200, json={"messages": [{"id": "wamid.H"}]})

    channel, _ = _channel(handler)
    assert channel.send_hello_world("+2348000000001").status == "sent"
    assert bodies[0]["template"] == {"name": "hello_world", "language": {"code": "en_US"}}


def test_channel_needs_credentials_and_a_valid_mode() -> None:
    with pytest.raises(ValueError):
        WhatsAppChannel(token="", phone_number_id="1")
    with pytest.raises(ValueError):
        WhatsAppChannel(token="t", phone_number_id="1", mode="sms")


def test_console_channel_masks_the_number() -> None:
    printed: list[str] = []
    result = ConsoleChannel(printer=printed.append).send("+2348000000001", _message())
    assert result.status == "dry_run"
    assert "+234******0001" in printed[0] and "2348000000001" not in printed[0]


# -- webhooks ------------------------------------------------------------------


def _sign(secret: str, body: bytes) -> str:
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def test_signatures_are_checked() -> None:
    body = b'{"entry": []}'
    assert verify_signature("s3cret", body, _sign("s3cret", body))
    assert not verify_signature("s3cret", body, _sign("other", body))
    assert not verify_signature("s3cret", body, None)
    assert not verify_signature("s3cret", body + b" ", _sign("s3cret", body))


@pytest.mark.parametrize(
    ("text", "kind"),
    [
        ("1", "followed"),
        (" Yes ", "followed"),
        ("2", "not_followed"),
        ("STOP", "stop"),
        ("Stop.", "stop"),
        ("start", "start"),
        ("the generator broke", "other"),
    ],
)
def test_replies_are_classified(text: str, kind: str) -> None:
    assert classify_reply(text) == kind


def _payload(
    *, statuses: list[dict[str, object]] = (), messages: list[dict[str, object]] = ()
) -> dict[str, object]:  # type: ignore[assignment]
    return {"entry": [{"changes": [{"value": {"statuses": statuses, "messages": messages}}]}]}


def test_webhooks_track_receipts_replies_and_opt_outs() -> None:
    store = PlanStore("duckdb:///:memory:")
    who = recipient_hash("+2348000000001")
    store.record_delivery(
        operator_id="op",
        site_id="s1",
        plan_date=date(2026, 3, 10),
        channel="whatsapp",
        recipient_hash=who,
        recipient_masked="+234******0001",
        status="sent",
        provider_message_id="wamid.1",
        error=None,
        attempts=1,
        at=datetime(2026, 3, 10, 5),
    )

    outcome = handle_webhook(
        store,
        _payload(
            statuses=[
                {"id": "wamid.1", "status": "read", "timestamp": "1773120000"},
                {"id": "wamid.1", "status": "delivered", "timestamp": "1773119000"},
            ]
        ),
    )
    assert outcome.statuses == 2
    assert store.deliveries("op")["status"].tolist() == ["read"]  # never moves backwards

    reply = {
        "id": "wamid.in1",
        "from": "2348000000001",
        "type": "text",
        "text": {"body": "1"},
        "timestamp": "1773121000",
    }
    assert handle_webhook(store, _payload(messages=[reply])).replies == 1
    assert handle_webhook(store, _payload(messages=[reply])).replies == 0  # retried webhook
    feedback = store.feedback("op")
    assert feedback["kind"].tolist() == ["followed"]
    assert str(feedback["site_id"].iloc[0]) == "s1"

    stop = {
        "id": "wamid.in2",
        "from": "2348000000001",
        "type": "button",
        "button": {"text": "STOP", "payload": "STOP"},
        "timestamp": "1773122000",
    }
    assert handle_webhook(store, _payload(messages=[stop])).opt_outs == 1
    assert store.is_opted_out(who)
    start = {
        "id": "wamid.in3",
        "from": "2348000000001",
        "type": "text",
        "text": {"body": "START"},
        "timestamp": "1773123000",
    }
    assert handle_webhook(store, _payload(messages=[start])).opt_ins == 1
    assert not store.is_opted_out(who)
