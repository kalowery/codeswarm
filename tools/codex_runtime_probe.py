#!/usr/bin/env python3
import argparse
import json
import select
import shutil
import subprocess
import tempfile
import time
import sys
from pathlib import Path


ROLLOUT_MARKER = "failed to record rollout items: failed to queue rollout items: channel closed"
DEFAULT_SANDBOX = "danger-full-access" if sys.platform == "darwin" else "workspace-write"


def jsonrpc_request(id_, method, params=None):
    payload = {"jsonrpc": "2.0", "id": id_, "method": method}
    if params is not None:
        payload["params"] = params
    return json.dumps(payload)


def jsonrpc_notification(method, params=None):
    payload = {"jsonrpc": "2.0", "method": method}
    if params is not None:
        payload["params"] = params
    return json.dumps(payload)


def write_worker_like_config(workspace: Path, sandbox_mode: str = DEFAULT_SANDBOX, network_access: bool = True):
    codex_dir = workspace / ".codex"
    codex_dir.mkdir(parents=True, exist_ok=True)
    lines = [
        'approval_policy = "never"',
        f'sandbox_mode = "{sandbox_mode}"',
        "",
    ]
    if sandbox_mode == "workspace-write":
        lines.extend([
            "[sandbox_workspace_write]",
            f"network_access = {str(bool(network_access)).lower()}",
            "",
        ])
    (codex_dir / "config.toml").write_text("\n".join(lines), encoding="utf-8")


def _poll_streams(proc, timeout_s: float):
    streams = []
    if proc.stdout and not proc.stdout.closed:
        streams.append(("stdout", proc.stdout))
    if proc.stderr and not proc.stderr.closed:
        streams.append(("stderr", proc.stderr))
    if not streams:
        time.sleep(timeout_s)
        return []
    ready, _, _ = select.select([stream for _, stream in streams], [], [], timeout_s)
    events = []
    for stream in ready:
        name = "stdout" if proc.stdout is stream else "stderr"
        line = stream.readline()
        if line:
            events.append((name, line.rstrip("\n")))
    return events


def wait_for_json_result(proc, request_id: int, stderr_lines: list[str], stdout_events: list[dict], timeout_s: float = 20.0):
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        for name, text in _poll_streams(proc, 0.2):
            if name == "stderr":
                stderr_lines.append(text)
                continue
            try:
                payload = json.loads(text)
            except Exception:
                continue
            stdout_events.append(payload)
            if payload.get("id") == request_id:
                return payload
    raise RuntimeError(f"Timed out waiting for JSON-RPC response id={request_id}")


def drain_streams(proc, stderr_lines: list[str], stdout_events: list[dict], duration_s: float = 3.0):
    deadline = time.time() + duration_s
    while time.time() < deadline:
        events = _poll_streams(proc, 0.2)
        if not events:
            continue
        for name, text in events:
            if name == "stderr":
                stderr_lines.append(text)
            else:
                try:
                    stdout_events.append(json.loads(text))
                except Exception:
                    pass


def run_case(label: str, config_mode: str):
    workspace = Path(tempfile.mkdtemp(prefix=f"codex-runtime-probe-{label}-"))
    (workspace / "README.txt").write_text("runtime probe\n", encoding="utf-8")
    if config_mode == "worker":
        write_worker_like_config(workspace, sandbox_mode=DEFAULT_SANDBOX, network_access=True)

    codex_bin = shutil.which("codex") or "codex"
    cmd = [
        codex_bin,
        "--cd",
        str(workspace),
        "--add-dir",
        str(Path.home() / ".codex"),
        "--add-dir",
        str(workspace),
        "app-server",
        "--listen",
        "stdio://",
    ]
    proc = subprocess.Popen(
        cmd,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        bufsize=1,
    )
    stderr_lines: list[str] = []
    stdout_events: list[dict] = []
    session_path = None
    thread_id = None

    try:
        def send(text: str):
            proc.stdin.write(text + "\n")
            proc.stdin.flush()

        send(jsonrpc_request(0, "initialize", {
            "processId": None,
            "rootUri": None,
            "capabilities": {"experimentalApi": True},
            "clientInfo": {
                "name": "codeswarm-runtime-probe",
                "title": "Codeswarm Runtime Probe",
                "version": "0.1.0",
            },
        }))
        wait_for_json_result(proc, 0, stderr_lines, stdout_events, timeout_s=20.0)
        send(jsonrpc_notification("initialized", {}))

        send(jsonrpc_request(1, "thread/start", {}))
        thread_result = wait_for_json_result(proc, 1, stderr_lines, stdout_events, timeout_s=20.0)
        thread = (thread_result.get("result") or {}).get("thread") or {}
        thread_id = thread.get("id")
        thread_path = thread.get("path")
        if isinstance(thread_path, str) and thread_path:
            session_path = Path(thread_path)

        if isinstance(thread_id, str) and thread_id:
            send(jsonrpc_request(2, "turn/start", {
                "threadId": thread_id,
                "input": [{"type": "text", "text": "Reply with OK only."}],
            }))
            wait_for_json_result(proc, 2, stderr_lines, stdout_events, timeout_s=20.0)

        drain_streams(proc, stderr_lines, stdout_events, duration_s=4.0)

        session_exists = bool(session_path and session_path.exists())
        session_tail = []
        if session_exists and session_path is not None:
            try:
                session_tail = session_path.read_text(encoding="utf-8").splitlines()[-8:]
            except Exception:
                session_tail = []

        return {
            "label": label,
            "workspace": str(workspace),
            "config_mode": config_mode,
            "config_path": str(workspace / ".codex" / "config.toml"),
            "config_present": (workspace / ".codex" / "config.toml").exists(),
            "stderr_log_path": str(workspace / "probe.stderr.log"),
            "rollout_marker_seen": any(ROLLOUT_MARKER in line for line in stderr_lines),
            "stderr_lines": stderr_lines[-20:],
            "stdout_event_count": len(stdout_events),
            "thread_id": thread_id,
            "session_path": str(session_path) if session_path else None,
            "session_exists": session_exists,
            "session_tail": session_tail,
        }
    finally:
        try:
            (workspace / "probe.stderr.log").write_text("\n".join(stderr_lines) + ("\n" if stderr_lines else ""), encoding="utf-8")
        except Exception:
            pass
        try:
            if proc.poll() is None:
                proc.terminate()
                proc.wait(timeout=5)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--case", choices=["none", "worker", "both"], default="both")
    args = parser.parse_args()

    cases = []
    if args.case in ("none", "both"):
        cases.append(run_case("no-project-config", "none"))
    if args.case in ("worker", "both"):
        cases.append(run_case("worker-local-config", "worker"))

    print(json.dumps({"cases": cases}, indent=2))


if __name__ == "__main__":
    main()
