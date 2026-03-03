from __future__ import annotations

from .config import Settings
from .models import InboundEmail, ProcessEmailResponse, ReferenceLine
from .providers.llm import LLMProvider
from .providers.messaging import MessagingProvider
from .retrieval import KnowledgeBase
from .storage import Storage


class EmailAnalysisService:
    def __init__(
        self,
        settings: Settings,
        storage: Storage,
        knowledge_base: KnowledgeBase,
        llm: LLMProvider,
        messaging: MessagingProvider,
    ):
        self.settings = settings
        self.storage = storage
        self.knowledge_base = knowledge_base
        self.llm = llm
        self.messaging = messaging

    def _target_recipients(self, email: InboundEmail) -> list[str]:
        recipients = {r.strip().lower() for r in [*email.to, *email.cc] if r.strip()}
        recipients.discard(self.settings.bot_email)
        return sorted(recipients)

    def _compose_summary_text(self, refs: list[ReferenceLine]) -> str:
        lines = [f"- {r.reference} — {r.relevance}" for r in refs]
        return "\n".join(lines)

    def process_email(self, email: InboundEmail, dry_run: bool = True) -> ProcessEmailResponse:
        recipients = self._target_recipients(email)

        query = self.llm.extract_query(email)
        sources = self.knowledge_base.search(query, top_k=self.settings.top_k)
        refs = self.llm.generate_reference_lines(email, sources)

        summary_text = self._compose_summary_text(refs)
        run_id = self.storage.create_run(email, recipients, summary_text, refs)

        if not dry_run:
            for recipient in recipients:
                ok, details = self.messaging.send(recipient, summary_text)
                self.storage.log_outbound(
                    run_id=run_id,
                    recipient=recipient,
                    channel=self.messaging.channel,
                    status="sent" if ok else "failed",
                    details=details,
                )

        return ProcessEmailResponse(
            run_id=run_id,
            recipients=recipients,
            summary_text=summary_text,
            references=refs,
        )
