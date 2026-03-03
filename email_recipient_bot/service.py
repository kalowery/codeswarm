from __future__ import annotations

from .config import Settings
from .forwarded_parser import parse_forwarded_email
from .models import InboundEmail, ProcessEmailResponse, RecipientRecommendation, ReferenceLine
from .providers.digest_email import DigestEmailProvider
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
        digest_email: DigestEmailProvider,
    ):
        self.settings = settings
        self.storage = storage
        self.knowledge_base = knowledge_base
        self.llm = llm
        self.messaging = messaging
        self.digest_email = digest_email

    def _target_recipients(self, email: InboundEmail) -> list[str]:
        recipients = {r.strip().lower() for r in [*email.to, *email.cc] if r.strip()}
        recipients.discard(self.settings.bot_email)
        return sorted(recipients)

    def _compose_summary_text(self, refs: list[ReferenceLine]) -> str:
        lines = [f"- {r.reference} — {r.relevance}" for r in refs]
        return "\n".join(lines)

    def _compose_digest_text(
        self,
        analysis_email: InboundEmail,
        recs: list[RecipientRecommendation],
        refs: list[ReferenceLine],
    ) -> str:
        lines = [
            f"Forwarded email digest for subject: {analysis_email.subject}",
            "",
            "Recommended recipient responses:",
        ]
        for r in recs:
            lines.append(f"- {r.recipient}: {r.recommended_response}")

        lines.append("")
        lines.append("Relevant references:")
        for ref in refs:
            lines.append(f"- {ref.reference} — {ref.relevance}")
        return "\n".join(lines).strip()

    def process_email(self, email: InboundEmail, dry_run: bool = True) -> ProcessEmailResponse:
        forwarded = parse_forwarded_email(email.body)

        if forwarded.detected and forwarded.body:
            analysis_email = InboundEmail(
                subject=forwarded.subject or email.subject,
                body=forwarded.body,
                sender=email.sender,
                to=forwarded.to_recipients,
                cc=forwarded.cc_recipients,
            )
            recipients = sorted({*forwarded.to_recipients, *forwarded.cc_recipients})
        else:
            analysis_email = email
            recipients = self._target_recipients(email)

        query = self.llm.extract_query(analysis_email)
        sources = self.knowledge_base.search(query, top_k=self.settings.top_k)
        refs = self.llm.generate_reference_lines(analysis_email, sources)
        recs = self.llm.generate_recipient_recommendations(analysis_email, recipients, refs)

        summary_text = self._compose_summary_text(refs)
        run_id = self.storage.create_run(analysis_email, recipients, summary_text, refs)

        if not dry_run:
            if forwarded.detected:
                digest_subject = f"Digest: Recommended responses for '{analysis_email.subject}'"
                digest_body = self._compose_digest_text(analysis_email, recs, refs)
                ok, details = self.digest_email.send_digest(email.sender, digest_subject, digest_body)
                self.storage.log_outbound(
                    run_id=run_id,
                    recipient=email.sender,
                    channel=self.digest_email.channel,
                    status="sent" if ok else "failed",
                    details=details,
                )
            else:
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
            recipient_recommendations=recs,
            forwarded_detected=forwarded.detected,
        )
