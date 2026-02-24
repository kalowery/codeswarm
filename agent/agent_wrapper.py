import os
import json
import time
import socket
import subprocess
from pathlib import Path

RUN_DIR = Path(os.environ.get("CODEX_CLUSTER_RUN_DIR", "/tmp/codex_cluster"))
NODE_NAME = os.environ.get("SLURMD_NODENAME", socket.gethostname())
NODE_DIR = RUN_DIR / NODE_NAME
NODE_DIR.mkdir(parents=True, exist_ok=True)

INBOX = NODE_DIR / "inbox.jsonl"
OUTBOX = NODE_DIR / "outbox.jsonl"
HEARTBEAT = NODE_DIR / "heartbeat.json"
STATE = NODE_DIR / "state.json"

# Initial prompt for Codex agent
INITIAL_PROMPT = """
You are a distributed Codex worker node in an HPC cluster.

Rules:
- Never block waiting for interactive stdin.
- All input arrives via JSON messages from inbox.jsonl.
- When you need human input, emit a JSON message of type 'input_request'.
- Log all major steps as JSON messages.
- Be concise.
"""

# Launch codex process (placeholder command — adjust as needed)
proc = subprocess.Popen(
    ["codex-agent", "--non-interactive"],
    stdin=subprocess.PIPE,
    stdout=subprocess.PIPE,
    stderr=subprocess.STDOUT,
    text=True,
    bufsize=1
)

# Send initial prompt
proc.stdin.write(INITIAL_PROMPT + "\n")
proc.stdin.flush()


def append_json(path, obj):
    with open(path, "a") as f:
        f.write(json.dumps(obj) + "\n")


def update_heartbeat():
    with open(HEARTBEAT, "w") as f:
        json.dump({"timestamp": time.time()}, f)


def process_inbox():
    if not INBOX.exists():
        return []
    with open(INBOX, "r") as f:
        lines = f.readlines()
    open(INBOX, "w").close()  # clear after reading
    return [json.loads(l) for l in lines if l.strip()]


append_json(OUTBOX, {"type": "status", "msg": "agent_started", "node": NODE_NAME})

while True:
    update_heartbeat()

    # Process controller messages
    for msg in process_inbox():
        if msg.get("type") == "input_response":
            proc.stdin.write(msg.get("response", "") + "\n")
            proc.stdin.flush()

    # Read codex output non-blocking
    if proc.stdout.readable():
        line = proc.stdout.readline()
        if line:
            append_json(OUTBOX, {"type": "log", "node": NODE_NAME, "content": line.strip()})

    if proc.poll() is not None:
        append_json(OUTBOX, {"type": "status", "msg": "agent_exited", "code": proc.returncode})
        break

    time.sleep(1)
