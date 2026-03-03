from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from ..models import InboundEmail


class EmailProvider(Protocol):
    def fetch_by_id(self, message_id: str) -> InboundEmail:
        ...


@dataclass
class GmailProvider:
    """
    Stub adapter for Gmail API retrieval.

    In production, replace fetch_by_id() with OAuth-authenticated calls to:
    - users.messages.get
    - users.messages.attachments.get (if needed)
    """

    def fetch_by_id(self, message_id: str) -> InboundEmail:
        raise NotImplementedError(
            "Gmail provider fetch is not wired in this stub. "
            "Send normalized email payload directly to webhook for now."
        )


@dataclass
class GraphProvider:
    """
    Stub adapter for Microsoft Graph retrieval.

    In production, replace fetch_by_id() with token-authenticated calls to:
    - GET /users/{id}/messages/{message_id}
    """

    def fetch_by_id(self, message_id: str) -> InboundEmail:
        raise NotImplementedError(
            "Graph provider fetch is not wired in this stub. "
            "Send normalized email payload directly to webhook for now."
        )
