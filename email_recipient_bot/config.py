from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Settings:
    bot_email: str
    docs_root: Path
    url_catalog_path: Path
    sqlite_path: Path
    top_k: int
    openai_model: str
    openai_api_key: str | None
    twilio_account_sid: str | None
    twilio_auth_token: str | None
    twilio_from_number: str | None
    smtp_host: str | None
    smtp_port: int
    smtp_username: str | None
    smtp_password: str | None
    smtp_from_email: str | None


def get_settings() -> Settings:
    base_dir = Path(__file__).resolve().parent
    docs_root = Path(os.getenv("BOT_DOCS_ROOT", str(base_dir / "knowledge" / "docs"))).resolve()
    url_catalog_path = Path(
        os.getenv("BOT_URL_CATALOG", str(base_dir / "knowledge" / "urls.json"))
    ).resolve()
    sqlite_path = Path(
        os.getenv("BOT_SQLITE_PATH", str(base_dir / "bot_state.sqlite3"))
    ).resolve()

    return Settings(
        bot_email=os.getenv("BOT_EMAIL", "bot@example.com").lower(),
        docs_root=docs_root,
        url_catalog_path=url_catalog_path,
        sqlite_path=sqlite_path,
        top_k=int(os.getenv("BOT_TOP_K", "5")),
        openai_model=os.getenv("OPENAI_MODEL", "gpt-5-mini"),
        openai_api_key=os.getenv("OPENAI_API_KEY"),
        twilio_account_sid=os.getenv("TWILIO_ACCOUNT_SID"),
        twilio_auth_token=os.getenv("TWILIO_AUTH_TOKEN"),
        twilio_from_number=os.getenv("TWILIO_FROM_NUMBER"),
        smtp_host=os.getenv("SMTP_HOST"),
        smtp_port=int(os.getenv("SMTP_PORT", "587")),
        smtp_username=os.getenv("SMTP_USERNAME"),
        smtp_password=os.getenv("SMTP_PASSWORD"),
        smtp_from_email=os.getenv("SMTP_FROM_EMAIL"),
    )
