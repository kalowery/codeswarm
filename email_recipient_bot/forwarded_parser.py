from __future__ import annotations

import re
from dataclasses import dataclass

EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
FORWARD_MARKER_RE = re.compile(
    r"(?im)(begin forwarded message|forwarded message|^-+\s*original message\s*-+$)"
)


@dataclass
class ForwardedContext:
    detected: bool
    subject: str | None
    body: str | None
    to_recipients: list[str]
    cc_recipients: list[str]


def _extract_addresses(value: str) -> list[str]:
    return sorted({m.lower() for m in EMAIL_RE.findall(value)})


def parse_forwarded_email(body: str) -> ForwardedContext:
    marker = FORWARD_MARKER_RE.search(body)
    if marker:
        segment = body[marker.start():]
    else:
        # fallback: if body begins with forwarded-style headers
        segment = body

    lines = segment.splitlines()

    headers: dict[str, str] = {}
    body_start = None
    saw_header = False

    for idx, raw_line in enumerate(lines):
        line = raw_line.strip("\r")
        if not line.strip():
            if saw_header:
                body_start = idx + 1
                break
            continue

        if ":" in line:
            k, v = line.split(":", 1)
            key = k.strip().lower()
            if key in {"from", "to", "cc", "subject", "date"}:
                headers[key] = v.strip()
                saw_header = True
                continue

        if saw_header and raw_line.startswith((" ", "\t")):
            # continuation line for prior header (rare)
            if headers:
                last = list(headers.keys())[-1]
                headers[last] = (headers[last] + " " + line.strip()).strip()
            continue

        # If we already saw header-like lines and now hit non-header text,
        # this is likely body.
        if saw_header:
            body_start = idx
            break

    to_recipients = _extract_addresses(headers.get("to", ""))
    cc_recipients = _extract_addresses(headers.get("cc", ""))
    subject = headers.get("subject")

    if body_start is None:
        forwarded_body = None
    else:
        forwarded_body = "\n".join(lines[body_start:]).strip() or None

    detected = bool((marker is not None) or (subject and (to_recipients or cc_recipients)))

    return ForwardedContext(
        detected=detected,
        subject=subject,
        body=forwarded_body,
        to_recipients=to_recipients,
        cc_recipients=cc_recipients,
    )
