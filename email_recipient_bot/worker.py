from __future__ import annotations

import os
import time

from .models import InboundEmail
from .runtime import Runtime, build_runtime

POLL_SECONDS = float(os.getenv("BOT_WORKER_POLL_SECONDS", "1.5"))


def run_once(runtime: Runtime) -> bool:
    claimed = runtime.storage.claim_next_job()
    if not claimed:
        return False

    job_id, source, payload = claimed
    try:
        email = InboundEmail.model_validate(payload["email"])
        dry_run = bool(payload.get("dry_run", True))
        runtime.service.process_email(email=email, dry_run=dry_run)
        runtime.storage.mark_job_done(job_id)
        print(f"[worker] processed job_id={job_id} source={source}", flush=True)
        return True
    except Exception as exc:
        runtime.storage.mark_job_failed(job_id, str(exc))
        print(f"[worker] failed job_id={job_id} error={exc}", flush=True)
        return True


def main() -> None:
    runtime = build_runtime()
    print("[worker] started", flush=True)
    while True:
        processed = run_once(runtime)
        if not processed:
            time.sleep(POLL_SECONDS)


if __name__ == "__main__":
    main()
