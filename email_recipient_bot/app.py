from __future__ import annotations

from fastapi import FastAPI
from fastapi import HTTPException

from .models import EnqueueJobResponse, ProcessEmailRequest, ProcessEmailResponse, WebhookEnvelope
from .providers.email import GmailProvider, GraphProvider
from .runtime import build_runtime

runtime = build_runtime()
settings = runtime.settings
storage = runtime.storage
knowledge_base = runtime.knowledge_base
service = runtime.service
gmail_provider = GmailProvider()
graph_provider = GraphProvider()

app = FastAPI(title="Multi-Recipient Email Reference Bot", version="0.1.0")


@app.get("/health")
def health() -> dict:
    return {
        "ok": True,
        "docs_root": str(settings.docs_root),
        "url_catalog_path": str(settings.url_catalog_path),
        "sqlite_path": str(settings.sqlite_path),
        "messaging_channel": service.messaging.channel,
        "digest_email_channel": service.digest_email.channel,
    }


@app.post("/refresh-index")
def refresh_index() -> dict:
    knowledge_base.refresh()
    return {"ok": True, "indexed_sources": len(knowledge_base.sources)}


@app.post("/process-email", response_model=ProcessEmailResponse)
def process_email(req: ProcessEmailRequest) -> ProcessEmailResponse:
    return service.process_email(req.email, dry_run=req.dry_run)


@app.post("/ingest/email", response_model=EnqueueJobResponse)
def ingest_email(req: ProcessEmailRequest) -> EnqueueJobResponse:
    payload = req.model_dump()
    job_id = storage.enqueue_job(source="direct", payload=payload)
    return EnqueueJobResponse(job_id=job_id, source="direct", status="queued")


@app.post("/webhooks/gmail", response_model=EnqueueJobResponse)
def gmail_webhook(req: WebhookEnvelope) -> EnqueueJobResponse:
    payload: dict
    if req.email:
        payload = {"email": req.email.model_dump(), "dry_run": req.dry_run}
    elif req.message_id:
        try:
            email = gmail_provider.fetch_by_id(req.message_id)
        except NotImplementedError as exc:
            raise HTTPException(status_code=501, detail=str(exc)) from exc
        payload = {"email": email.model_dump(), "dry_run": req.dry_run}
    else:
        raise HTTPException(status_code=400, detail="Provide either email or message_id")

    job_id = storage.enqueue_job(source="gmail", payload=payload)
    return EnqueueJobResponse(job_id=job_id, source="gmail", status="queued")


@app.post("/webhooks/graph", response_model=EnqueueJobResponse)
def graph_webhook(req: WebhookEnvelope) -> EnqueueJobResponse:
    payload: dict
    if req.email:
        payload = {"email": req.email.model_dump(), "dry_run": req.dry_run}
    elif req.message_id:
        try:
            email = graph_provider.fetch_by_id(req.message_id)
        except NotImplementedError as exc:
            raise HTTPException(status_code=501, detail=str(exc)) from exc
        payload = {"email": email.model_dump(), "dry_run": req.dry_run}
    else:
        raise HTTPException(status_code=400, detail="Provide either email or message_id")

    job_id = storage.enqueue_job(source="graph", payload=payload)
    return EnqueueJobResponse(job_id=job_id, source="graph", status="queued")
