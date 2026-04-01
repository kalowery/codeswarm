#!/usr/bin/env python3
import argparse
import atexit
import json
import os
import shutil
import socket
import subprocess
import tempfile
import time
import uuid
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PROTOCOL = "codeswarm.router.v1"


def port_open(host: str, port: int) -> bool:
    try:
        with socket.create_connection((host, port), timeout=0.5):
            return True
    except OSError:
        return False


def wait_until(label: str, predicate, timeout: float = 180.0, interval: float = 0.2):
    deadline = time.time() + timeout
    while time.time() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(interval)
    raise RuntimeError(f"Timed out waiting for {label}")


class RouterClient:
    def __init__(self, host: str, port: int):
        self.sock = socket.create_connection((host, port), timeout=5)
        self.sock.settimeout(0.2)
        self.buffer = b""
        self.events: list[dict] = []

    def close(self):
        try:
            self.sock.close()
        except Exception:
            pass

    def send(self, command: str, payload: dict) -> str:
        request_id = str(uuid.uuid4())
        msg = {
            "protocol": PROTOCOL,
            "type": "command",
            "command": command,
            "request_id": request_id,
            "payload": payload,
        }
        self.sock.sendall((json.dumps(msg) + "\n").encode("utf-8"))
        return request_id

    def pump(self):
        while True:
            try:
                chunk = self.sock.recv(65536)
            except socket.timeout:
                break
            if not chunk:
                break
            self.buffer += chunk
            while b"\n" in self.buffer:
                line, self.buffer = self.buffer.split(b"\n", 1)
                line = line.strip()
                if not line:
                    continue
                msg = json.loads(line.decode("utf-8"))
                if msg.get("type") == "event":
                    self.events.append(msg)

    def wait_for_event(self, predicate, timeout: float = 600.0):
        deadline = time.time() + timeout
        while time.time() < deadline:
            self.pump()
            for idx, event in enumerate(self.events):
                if predicate(event):
                    return self.events.pop(idx)
            time.sleep(0.1)
        raise RuntimeError("Timed out waiting for router event")


def write_temp_aws_only_config(base_config_path: Path, provider_id: str) -> Path:
    config = json.loads(base_config_path.read_text(encoding="utf-8"))
    launch_providers = config.get("launch_providers") or []
    filtered = [
        provider
        for provider in launch_providers
        if isinstance(provider, dict) and str(provider.get("id") or "") == provider_id
    ]
    if not filtered:
        raise RuntimeError(f"Unable to find provider '{provider_id}' in {base_config_path}")
    cluster = dict(config.get("cluster") or {})
    aws_cfg = cluster.get("aws")
    if not isinstance(aws_cfg, dict):
        raise RuntimeError("Config does not contain cluster.aws")
    config["cluster"] = {"aws": aws_cfg}
    config["launch_providers"] = filtered
    tmp_root = Path(tempfile.mkdtemp(prefix="codeswarm-aws-only-"))
    path = tmp_root / "aws-only.config.json"
    path.write_text(json.dumps(config, indent=2), encoding="utf-8")
    return path


def start_router(config_path: Path, state_file: Path, pid_file: Path, host: str, port: int):
    env = {
        **os.environ,
        "CODESWARM_ROUTER_HOST": host,
        "CODESWARM_ROUTER_PORT": str(port),
        "CODESWARM_ROUTER_PID_FILE": str(pid_file),
        "CODESWARM_ROUTER_STATE_FILE": str(state_file),
        "CODESWARM_DISABLE_BEADS_SYNC": "1",
    }
    proc = subprocess.Popen(
        [os.environ.get("PYTHON", "python3.12"), "-u", "-m", "router.router", "--config", str(config_path), "--daemon"],
        cwd=str(ROOT),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        env=env,
    )
    wait_until("router", lambda: port_open(host, port), timeout=120.0)
    return proc


def terminate_process(proc):
    if proc is None:
        return
    try:
        proc.terminate()
        proc.wait(timeout=5)
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass


def main():
    parser = argparse.ArgumentParser(description="Run an AWS-only Claude runtime smoke without touching Slurm/HPC providers.")
    parser.add_argument("--config", default=str(ROOT / "configs" / "combined.json"))
    parser.add_argument("--provider", default="aws-claude-default")
    parser.add_argument("--nodes", type=int, default=1)
    parser.add_argument("--prompt", default="Reply with exactly READY and nothing else.")
    parser.add_argument("--launch-timeout", type=float, default=1800.0)
    args = parser.parse_args()

    if not str(os.environ.get("ANTHROPIC_API_KEY") or "").strip():
        raise RuntimeError("ANTHROPIC_API_KEY must be set for AWS Claude smoke")

    config_path = write_temp_aws_only_config(Path(args.config), args.provider)
    tmp_root = config_path.parent
    state_file = tmp_root / "router_state.json"
    pid_file = tmp_root / "router.pid"
    host = "127.0.0.1"
    with socket.socket() as s:
        s.bind((host, 0))
        port = int(s.getsockname()[1])

    router_proc = None
    client = None
    swarm_id = ""
    try:
        router_proc = start_router(config_path, state_file, pid_file, host, port)
        client = RouterClient(host, port)
        launch_request = client.send(
            "swarm_launch",
            {
                "provider": args.provider,
                "nodes": int(args.nodes),
                "system_prompt": args.prompt,
                "provider_params": {
                    "worker_mode": "claude",
                    "approval_policy": "never",
                    "workers_per_node": 1,
                    "node_count": 1,
                    "ebs_volume_size_gb": 8,
                    "delete_ebs_on_shutdown": True,
                },
            },
        )
        print(f"launch_request_id={launch_request}", flush=True)
        launched = client.wait_for_event(
            lambda e: e.get("event") == "swarm_launched"
            and str((e.get("data") or {}).get("request_id") or "") == launch_request,
            timeout=args.launch_timeout,
        )
        launch_data = launched.get("data") or {}
        swarm_id = str(launch_data.get("swarm_id") or "")
        print(f"swarm_id={swarm_id}", flush=True)

        assistant = client.wait_for_event(
            lambda e: e.get("event") == "assistant"
            and str((e.get("data") or {}).get("swarm_id") or "") == swarm_id,
            timeout=args.launch_timeout,
        )
        content = str(((assistant.get("data") or {}).get("content")) or "")
        print(f"assistant={content}", flush=True)
        if content.strip() != "READY":
            raise RuntimeError(f"Unexpected assistant content: {content!r}")

        terminate_request = client.send("swarm_terminate", {"swarm_id": swarm_id})
        print(f"terminate_request_id={terminate_request}", flush=True)
        client.wait_for_event(
            lambda e: e.get("event") == "swarm_terminated"
            and str((e.get("data") or {}).get("swarm_id") or "") == swarm_id,
            timeout=900.0,
        )
        print("termination=complete", flush=True)
    finally:
        if client is not None and swarm_id:
            try:
                client.send("swarm_terminate", {"swarm_id": swarm_id})
            except Exception:
                pass
            try:
                client.wait_for_event(
                    lambda e: e.get("event") == "swarm_terminated"
                    and str((e.get("data") or {}).get("swarm_id") or "") == swarm_id,
                    timeout=30.0,
                )
            except Exception:
                pass
        if client is not None:
            client.close()
        terminate_process(router_proc)
        shutil.rmtree(tmp_root, ignore_errors=True)


if __name__ == "__main__":
    main()
