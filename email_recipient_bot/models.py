from __future__ import annotations

from typing import Literal
from pydantic import BaseModel, Field


class InboundEmail(BaseModel):
    subject: str = Field(..., min_length=1)
    body: str = Field(..., min_length=1)
    sender: str = Field(..., min_length=3)
    to: list[str] = Field(default_factory=list)
    cc: list[str] = Field(default_factory=list)


class ReferenceLine(BaseModel):
    reference: str
    reference_type: Literal["url", "document"]
    relevance: str


class ProcessEmailRequest(BaseModel):
    email: InboundEmail
    dry_run: bool = True


class ProcessEmailResponse(BaseModel):
    run_id: str
    recipients: list[str]
    summary_text: str
    references: list[ReferenceLine]


class EnqueueJobResponse(BaseModel):
    job_id: str
    source: str
    status: str


class WebhookEnvelope(BaseModel):
    email: InboundEmail | None = None
    message_id: str | None = None
    dry_run: bool = True


class KnowledgeSource(BaseModel):
    reference: str
    reference_type: Literal["url", "document"]
    content: str
    title: str | None = None
    score: float = 0.0
