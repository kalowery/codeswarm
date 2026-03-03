from __future__ import annotations

from typing import Protocol


class MessagingProvider(Protocol):
    channel: str

    def send(self, recipient: str, body: str) -> tuple[bool, str]:
        ...


class ConsoleMessagingProvider:
    channel = "console"

    def send(self, recipient: str, body: str) -> tuple[bool, str]:
        print(f"[dispatch:console] recipient={recipient}\n{body}\n")
        return True, "printed_to_console"


class TwilioMessagingProvider:
    channel = "twilio_sms"

    def __init__(self, account_sid: str, auth_token: str, from_number: str):
        from twilio.rest import Client

        self.from_number = from_number
        self.client = Client(account_sid, auth_token)

    def send(self, recipient: str, body: str) -> tuple[bool, str]:
        try:
            msg = self.client.messages.create(
                from_=self.from_number,
                body=body,
                to=recipient,
            )
            return True, msg.sid
        except Exception as exc:
            return False, str(exc)


def build_messaging_provider(
    account_sid: str | None,
    auth_token: str | None,
    from_number: str | None,
) -> MessagingProvider:
    if account_sid and auth_token and from_number:
        try:
            return TwilioMessagingProvider(account_sid, auth_token, from_number)
        except Exception:
            pass
    return ConsoleMessagingProvider()
