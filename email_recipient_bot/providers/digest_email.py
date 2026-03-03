from __future__ import annotations

import smtplib
from email.message import EmailMessage
from typing import Protocol


class DigestEmailProvider(Protocol):
    channel: str

    def send_digest(self, recipient: str, subject: str, body: str) -> tuple[bool, str]:
        ...


class ConsoleDigestEmailProvider:
    channel = "console_email"

    def send_digest(self, recipient: str, subject: str, body: str) -> tuple[bool, str]:
        print(
            f"[dispatch:email-console] to={recipient} subject={subject}\n{body}\n",
            flush=True,
        )
        return True, "printed_to_console"


class SMTPDigestEmailProvider:
    channel = "smtp_email"

    def __init__(
        self,
        host: str,
        port: int,
        username: str,
        password: str,
        from_email: str,
    ):
        self.host = host
        self.port = port
        self.username = username
        self.password = password
        self.from_email = from_email

    def send_digest(self, recipient: str, subject: str, body: str) -> tuple[bool, str]:
        msg = EmailMessage()
        msg["From"] = self.from_email
        msg["To"] = recipient
        msg["Subject"] = subject
        msg.set_content(body)

        try:
            with smtplib.SMTP(self.host, self.port, timeout=20) as smtp:
                smtp.starttls()
                smtp.login(self.username, self.password)
                smtp.send_message(msg)
            return True, "smtp_sent"
        except Exception as exc:
            return False, str(exc)


def build_digest_email_provider(
    host: str | None,
    port: int,
    username: str | None,
    password: str | None,
    from_email: str | None,
) -> DigestEmailProvider:
    if host and username and password and from_email:
        return SMTPDigestEmailProvider(
            host=host,
            port=port,
            username=username,
            password=password,
            from_email=from_email,
        )
    return ConsoleDigestEmailProvider()
