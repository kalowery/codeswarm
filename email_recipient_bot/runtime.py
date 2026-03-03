from __future__ import annotations

from dataclasses import dataclass

from .config import Settings, get_settings
from .providers.digest_email import build_digest_email_provider
from .providers.llm import build_llm
from .providers.messaging import build_messaging_provider
from .retrieval import KnowledgeBase
from .service import EmailAnalysisService
from .storage import Storage


@dataclass
class Runtime:
    settings: Settings
    storage: Storage
    knowledge_base: KnowledgeBase
    service: EmailAnalysisService


def build_runtime() -> Runtime:
    settings = get_settings()
    storage = Storage(settings.sqlite_path)
    knowledge_base = KnowledgeBase(settings.docs_root, settings.url_catalog_path)
    knowledge_base.refresh()
    llm = build_llm(settings.openai_api_key, settings.openai_model)
    messaging = build_messaging_provider(
        settings.twilio_account_sid,
        settings.twilio_auth_token,
        settings.twilio_from_number,
    )
    digest_email = build_digest_email_provider(
        settings.smtp_host,
        settings.smtp_port,
        settings.smtp_username,
        settings.smtp_password,
        settings.smtp_from_email,
    )
    service = EmailAnalysisService(
        settings=settings,
        storage=storage,
        knowledge_base=knowledge_base,
        llm=llm,
        messaging=messaging,
        digest_email=digest_email,
    )
    return Runtime(
        settings=settings,
        storage=storage,
        knowledge_base=knowledge_base,
        service=service,
    )
