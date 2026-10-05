"""Who is asking, and which operator's data they may see.

One rule for the API and the dashboard: a credential is either Vaticore's
admin token (every operator) or an operator key from
`python -m vaticore.pipeline apikey create` (that operator only). Keys are
stored hashed; the admin token is compared in constant time.
"""

from __future__ import annotations

import hmac
from dataclasses import dataclass

from vaticore.config import Settings
from vaticore.pipeline.store import PlanStore


@dataclass(frozen=True)
class Access:
    operator_id: str | None  # None: the admin, every operator

    @property
    def admin(self) -> bool:
        return self.operator_id is None

    def may_see(self, operator_id: str) -> bool:
        return self.admin or self.operator_id == operator_id

    @property
    def label(self) -> str:
        return "Vaticore admin" if self.admin else f"operator {self.operator_id}"


def resolve(credential: str, settings: Settings, store: PlanStore) -> Access | None:
    """The access a credential grants, or None if it is unknown or revoked."""
    token = credential.removeprefix("Bearer ").strip()
    if not token:
        return None
    admin = settings.api_token
    if admin is not None and hmac.compare_digest(token, admin.get_secret_value()):
        return Access(None)
    owner = store.operator_for_key(token)
    return None if owner is None else Access(owner)


def open_without_credentials(settings: Settings) -> bool:
    """Local development with no admin token set: a demo works out of the box."""
    return settings.api_token is None and settings.environment == "local"
