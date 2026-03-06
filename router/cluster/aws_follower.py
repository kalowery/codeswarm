#!/usr/bin/env python3
import argparse
import json
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path


def load_state(path: Path) -> dict:
    try:
        if not path.exists():
            return {"jobs": {}}
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return {"jobs": {}}
        jobs = data.get("jobs")
        if not isinstance(jobs, dict):
            return {"jobs": {}}
        return data
    except Exception:
        return {"jobs": {}}


def spawn_tail(job_id: str, job_meta: dict) -> subprocess.Popen:
    host = str(job_meta.get("coordinator_host") or "").strip()
    ssh_user = str(job_meta.get("ssh_user") or "ubuntu").strip()
    key_path = str(job_meta.get("ssh_private_key_path") or "").strip()
    base_path = str(job_meta.get("base_path") or "").strip()

    if not host or not key_path or not base_path:
        raise RuntimeError(f"Job {job_id} missing follower metadata")

    remote_cmd = f"python3 {base_path}/agent/outbox_follower.py {base_path}/mailbox/outbox"

    cmd = [
        "ssh",
        "-i",
        key_path,
        "-o",
        "BatchMode=yes",
        "-o",
        "ConnectTimeout=10",
        "-o",
        "StrictHostKeyChecking=no",
        "-o",
        "UserKnownHostsFile=/dev/null",
        f"{ssh_user}@{host}",
        remote_cmd,
    ]

    return subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        bufsize=1,
        text=True,
    )


def pump_stdout(job_id: str, proc: subprocess.Popen, shutdown_event: threading.Event):
    try:
        if proc.stdout is None:
            return
        for line in proc.stdout:
            if shutdown_event.is_set():
                return
            if line:
                print(line.rstrip("\n"), flush=True)
    except Exception:
        return


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--state-file", required=True)
    parser.add_argument("--poll-seconds", type=float, default=3.0)
    args = parser.parse_args()

    state_path = Path(args.state_file)
    poll_seconds = max(1.0, float(args.poll_seconds))

    shutdown_event = threading.Event()

    def _handle_signal(_sig, _frame):
        shutdown_event.set()

    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)

    tails: dict[str, subprocess.Popen] = {}
    pumps: dict[str, threading.Thread] = {}

    while not shutdown_event.is_set():
        state = load_state(state_path)
        jobs = state.get("jobs") if isinstance(state, dict) else {}
        if not isinstance(jobs, dict):
            jobs = {}

        active_job_ids = {
            str(job_id)
            for job_id, meta in jobs.items()
            if isinstance(meta, dict) and str(meta.get("status") or "running") != "terminated"
        }

        for job_id in active_job_ids:
            if job_id in tails and tails[job_id].poll() is None:
                continue

            if job_id in tails:
                old = tails.pop(job_id)
                try:
                    old.terminate()
                except Exception:
                    pass

            try:
                proc = spawn_tail(job_id, jobs[job_id])
            except Exception as e:
                print(f"[aws_follower] failed to start tail for {job_id}: {e}", file=sys.stderr, flush=True)
                continue

            tails[job_id] = proc
            t = threading.Thread(target=pump_stdout, args=(job_id, proc, shutdown_event), daemon=True)
            pumps[job_id] = t
            t.start()

        for job_id in list(tails.keys()):
            proc = tails[job_id]
            if job_id not in active_job_ids:
                try:
                    proc.terminate()
                except Exception:
                    pass
                tails.pop(job_id, None)
                pumps.pop(job_id, None)
                continue

            if proc.poll() is not None:
                # Process died; remove and retry on next poll.
                tails.pop(job_id, None)
                pumps.pop(job_id, None)

        time.sleep(poll_seconds)

    for proc in tails.values():
        try:
            proc.terminate()
        except Exception:
            pass


if __name__ == "__main__":
    main()
