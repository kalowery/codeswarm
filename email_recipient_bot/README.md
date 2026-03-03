# Multi-Recipient Email Reference Bot (Stub)

This is a runnable stub for the architecture discussed:

- Inbound email analysis
- Gmail/Graph webhook ingress stubs
- Queue + worker processing split
- Retrieval from file-system docs and URL catalog
- Optional OpenAI-assisted intent/query extraction and relevance generation
- Outbound dispatch via console (default) or Twilio SMS
- Forwarded-email detection with digest reply-to-sender workflow
- Persistent run/outbound logs in SQLite

## Run

```bash
python3 -m venv .venv
source .venv/bin/activate  # Windows PowerShell: .venv\Scripts\Activate.ps1
pip install -r email_recipient_bot/requirements.txt
uvicorn email_recipient_bot.app:app --reload --port 8090
```

Run worker in a second shell:

```bash
python -m email_recipient_bot.worker
```

## One-command Docker startup

From repo root:

```bash
docker compose -f email_recipient_bot/docker-compose.yml up --build
```

## Environment Variables

- `BOT_EMAIL` (default: `bot@example.com`)
- `BOT_DOCS_ROOT` (default: `email_recipient_bot/knowledge/docs`)
- `BOT_URL_CATALOG` (default: `email_recipient_bot/knowledge/urls.json`)
- `BOT_SQLITE_PATH` (default: `email_recipient_bot/bot_state.sqlite3`)
- `BOT_TOP_K` (default: `5`)
- `OPENAI_API_KEY` (optional)
- `OPENAI_MODEL` (default: `gpt-5-mini`)
- `TWILIO_ACCOUNT_SID` (optional)
- `TWILIO_AUTH_TOKEN` (optional)
- `TWILIO_FROM_NUMBER` (optional)
- `SMTP_HOST` (optional)
- `SMTP_PORT` (default: `587`)
- `SMTP_USERNAME` (optional)
- `SMTP_PASSWORD` (optional)
- `SMTP_FROM_EMAIL` (optional)
- `BOT_WORKER_POLL_SECONDS` (worker only; default: `1.5`)

If Twilio creds are not set, outbound messages are printed to console.
If SMTP creds are not set, digest emails are printed to console.

## API

### `GET /health`

Returns service and provider status.

### `POST /refresh-index`

Reloads file/URL knowledge sources.

### `POST /process-email`

Request:

```json
{
  "dry_run": true,
  "email": {
    "subject": "Need rollout references",
    "body": "Please send docs on API limits and billing retries.",
    "sender": "lead@example.com",
    "to": ["bot@example.com", "ops@example.com"],
    "cc": ["finance@example.com"]
  }
}
```

Response includes:

- `run_id`
- recipient list excluding bot mailbox
- one-line cited summaries (reference + one sentence relevance)
- per-recipient recommended response lines
- `forwarded_detected` flag

### `POST /ingest/email`

Queues a normalized email payload for worker processing.

### `POST /webhooks/gmail`

Webhook ingress for Gmail events. For this stub, send either:

- `email` (normalized body), or
- `message_id` (fetch-by-id is intentionally unimplemented stub)

### `POST /webhooks/graph`

Webhook ingress for Microsoft Graph events. Same payload contract as Gmail webhook.

## Notes

- This is intentionally a stub, not production-hardened.
- Graph/Gmail provider fetch adapters are intentionally left as `NotImplementedError`.
- Production extensions should add OAuth mailbox fetch, queue durability/locking hardening, ACL enforcement, and structured observability.

## Forwarded email behavior

When forwarded content is detected in an inbound message body (for example `Begin forwarded message` or `Original Message` header blocks), the service:

1. Extracts original subject/body and original `To`/`Cc` recipients.
2. Produces references plus a one-sentence recommended response for each original recipient.
3. On `dry_run=false`, sends a single digest email back to the forwarder (`email.sender`) containing per-recipient recommendations and citations.
