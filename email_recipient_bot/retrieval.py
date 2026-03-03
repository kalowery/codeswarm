from __future__ import annotations

import json
import re
from pathlib import Path

from .models import KnowledgeSource

TOKEN_RE = re.compile(r"[a-zA-Z0-9_]{3,}")


def tokenize(text: str) -> set[str]:
    return {t.lower() for t in TOKEN_RE.findall(text)}


class KnowledgeBase:
    def __init__(self, docs_root: Path, url_catalog_path: Path):
        self.docs_root = docs_root
        self.url_catalog_path = url_catalog_path
        self.sources: list[KnowledgeSource] = []

    def refresh(self) -> None:
        self.sources = []
        self.sources.extend(self._load_docs())
        self.sources.extend(self._load_urls())

    def _load_docs(self) -> list[KnowledgeSource]:
        out: list[KnowledgeSource] = []
        if not self.docs_root.exists():
            return out

        for path in sorted(self.docs_root.rglob("*")):
            if not path.is_file():
                continue
            if path.suffix.lower() not in {".txt", ".md", ".rst"}:
                continue

            try:
                content = path.read_text(encoding="utf-8", errors="ignore")
            except OSError:
                continue

            out.append(
                KnowledgeSource(
                    reference=str(path),
                    reference_type="document",
                    content=content,
                    title=path.name,
                )
            )
        return out

    def _load_urls(self) -> list[KnowledgeSource]:
        out: list[KnowledgeSource] = []
        if not self.url_catalog_path.exists():
            return out

        try:
            raw = json.loads(self.url_catalog_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return out

        for item in raw:
            url = item.get("url")
            if not url:
                continue
            out.append(
                KnowledgeSource(
                    reference=url,
                    reference_type="url",
                    content=item.get("content", ""),
                    title=item.get("title"),
                )
            )
        return out

    def search(self, query: str, top_k: int = 5) -> list[KnowledgeSource]:
        q_tokens = tokenize(query)
        scored: list[KnowledgeSource] = []

        for src in self.sources:
            corpus = " ".join([src.title or "", src.content, src.reference])
            overlap = q_tokens.intersection(tokenize(corpus))
            if not overlap:
                continue

            score = len(overlap) / max(len(q_tokens), 1)
            scored.append(src.model_copy(update={"score": score}))

        scored.sort(key=lambda s: s.score, reverse=True)
        return scored[:top_k]
