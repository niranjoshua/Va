"""Who receives each site's plan, with their consent.

Recipients live in a TOML file outside the repository (phone numbers are
personal data; see examples/sites/recipients.example.toml). WhatsApp only
allows business-initiated messages to people who agreed to receive them, so
every recipient must have consent = true, recorded with when and how it was
given. A reply of STOP is stored in the plan store and always wins over this
file.

    [[recipient]]
    name = "Site manager, Ikorodu"
    whatsapp = "+2348000000001"
    operator_id = "example-towerco"
    sites = ["lag-ikd-0142"]          # or ["*"] for every site of the operator
    consent = true
    consent_note = "Agreed at pilot kick-off, 2026-10-06"
"""

from __future__ import annotations

import hashlib
import re
import tomllib
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

_E164 = re.compile(r"^\+[1-9]\d{7,14}$")


def normalise_phone(value: str) -> str:
    """E.164 form, '+' and digits only; spaces, dashes and brackets removed."""
    cleaned = re.sub(r"[\s\-().]", "", value)
    if cleaned.startswith("00"):
        cleaned = "+" + cleaned[2:]
    if not cleaned.startswith("+") and cleaned.isdigit():
        cleaned = "+" + cleaned
    if not _E164.match(cleaned):
        raise ValueError(f"not an international phone number: {value!r} (use +234...)")
    return cleaned


def recipient_hash(phone: str) -> str:
    """One-way identifier for a phone number: matches replies without storing it."""
    digits = normalise_phone(phone).lstrip("+")
    return hashlib.sha256(f"vaticore-recipient:{digits}".encode()).hexdigest()


def mask_phone(phone: str) -> str:
    """+2348012345678 -> +234******5678, safe for logs and reports."""
    number = normalise_phone(phone)
    return number[:4] + "*" * (len(number) - 8) + number[-4:]


_EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def normalise_email(value: str) -> str:
    cleaned = value.strip().lower()
    if not _EMAIL.match(cleaned):
        raise ValueError(f"not an email address: {value!r}")
    return cleaned


def email_hash(email: str) -> str:
    """One-way identifier for an email address, like recipient_hash for phones."""
    return hashlib.sha256(f"vaticore-email:{normalise_email(email)}".encode()).hexdigest()


def mask_email(email: str) -> str:
    """ada.obi@bank.ng -> a******@bank.ng"""
    local, _, domain = normalise_email(email).partition("@")
    return f"{local[0]}{'*' * max(len(local) - 1, 1)}@{domain}"


class Recipient(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str = Field(min_length=1)
    whatsapp: str | None = None
    email: str | None = None
    operator_id: str = Field(min_length=1)
    sites: tuple[str, ...] = Field(min_length=1)
    consent: bool
    consent_note: str | None = None
    language: str = "en"

    @field_validator("whatsapp")
    @classmethod
    def _phone(cls, value: str | None) -> str | None:
        return None if value is None else normalise_phone(value)

    @field_validator("email")
    @classmethod
    def _email(cls, value: str | None) -> str | None:
        return None if value is None else normalise_email(value)

    @model_validator(mode="after")
    def _reachable(self) -> Recipient:
        if self.whatsapp is None and self.email is None:
            raise ValueError(f"recipient {self.name!r} needs a whatsapp number or an email")
        return self

    @property
    def hashes(self) -> tuple[str, ...]:
        """One-way identifiers for every address: an opt-out on any one stops all."""
        out = []
        if self.whatsapp:
            out.append(recipient_hash(self.whatsapp))
        if self.email:
            out.append(email_hash(self.email))
        return tuple(out)

    @property
    def hash(self) -> str:
        return self.hashes[0]

    @property
    def masked(self) -> str:
        return mask_phone(self.whatsapp) if self.whatsapp else mask_email(self.email or "")

    def covers(self, operator_id: str, site_id: str) -> bool:
        return self.operator_id == operator_id and ("*" in self.sites or site_id in self.sites)


def load_recipients(path: str | Path) -> list[Recipient]:
    """Read and validate a recipients file."""
    data: dict[str, Any] = tomllib.loads(Path(path).read_text(encoding="utf-8"))
    return [Recipient(**item) for item in data.get("recipient", [])]


def recipients_for(recipients: list[Recipient], operator_id: str, site_id: str) -> list[Recipient]:
    """Consenting recipients for one site. People without consent are never messaged."""
    return [r for r in recipients if r.consent and r.covers(operator_id, site_id)]
