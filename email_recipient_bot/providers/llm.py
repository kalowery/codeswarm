from __future__ import annotations

import json
import re
from typing import Protocol

from ..models import InboundEmail, KnowledgeSource, ReferenceLine


SENTENCE_RE = re.compile(r"\s+")


def _normalize_sentence(text: str) -> str:
    text = SENTENCE_RE.sub(" ", text.strip())
    if not text.endswith("."):
        text += "."
    return text


class LLMProvider(Protocol):
    def extract_query(self, email: InboundEmail) -> str:
        ...

    def generate_reference_lines(
        self,
        email: InboundEmail,
        sources: list[KnowledgeSource],
    ) -> list[ReferenceLine]:
        ...


class HeuristicLLM:
    def extract_query(self, email: InboundEmail) -> str:
        return f"{email.subject}\n{email.body}"

    def generate_reference_lines(
        self,
        email: InboundEmail,
        sources: list[KnowledgeSource],
    ) -> list[ReferenceLine]:
        lines: list[ReferenceLine] = []
        for src in sources:
            snippet = (src.content or "").strip().splitlines()[0] if src.content else ""
            phrase = snippet[:140] if snippet else (src.title or src.reference)
            relevance = _normalize_sentence(
                f"Relevant because it addresses '{email.subject}' and includes: {phrase}"
            )
            lines.append(
                ReferenceLine(
                    reference=src.reference,
                    reference_type=src.reference_type,
                    relevance=relevance,
                )
            )
        return lines


class OpenAILLM:
    def __init__(self, api_key: str, model: str):
        from openai import OpenAI

        self.client = OpenAI(api_key=api_key)
        self.model = model
        self.fallback = HeuristicLLM()

    def extract_query(self, email: InboundEmail) -> str:
        prompt = (
            "Extract concise search intent for retrieval from this email. "
            "Return plain text only with key entities, products, constraints, and asks."
        )
        try:
            resp = self.client.responses.create(
                model=self.model,
                input=[
                    {"role": "system", "content": prompt},
                    {
                        "role": "user",
                        "content": f"Subject: {email.subject}\n\nBody:\n{email.body}",
                    },
                ],
                temperature=0,
            )
            text = resp.output_text.strip()
            return text or self.fallback.extract_query(email)
        except Exception:
            return self.fallback.extract_query(email)

    def generate_reference_lines(
        self,
        email: InboundEmail,
        sources: list[KnowledgeSource],
    ) -> list[ReferenceLine]:
        if not sources:
            return []

        payload = [
            {
                "reference": s.reference,
                "reference_type": s.reference_type,
                "title": s.title,
                "content": s.content[:2000],
            }
            for s in sources
        ]

        system = (
            "You are generating outbound references for a multi-recipient SMS/email bot. "
            "Output JSON only as a list of objects with keys: reference, reference_type, relevance. "
            "Each relevance value must be exactly one sentence and explain relevance to the inbound email. "
            "Only use references provided in the sources list."
        )

        user = (
            f"Inbound subject: {email.subject}\n"
            f"Inbound body: {email.body}\n\n"
            f"Sources JSON:\n{json.dumps(payload, ensure_ascii=True)}"
        )

        try:
            resp = self.client.responses.create(
                model=self.model,
                input=[
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
                temperature=0.1,
            )
            raw = resp.output_text.strip()
            parsed = json.loads(raw)
            out: list[ReferenceLine] = []
            for item in parsed:
                out.append(
                    ReferenceLine(
                        reference=item["reference"],
                        reference_type=item["reference_type"],
                        relevance=_normalize_sentence(item["relevance"]),
                    )
                )
            return out
        except Exception:
            return self.fallback.generate_reference_lines(email, sources)


def build_llm(api_key: str | None, model: str) -> LLMProvider:
    if api_key:
        try:
            return OpenAILLM(api_key=api_key, model=model)
        except Exception:
            pass
    return HeuristicLLM()
