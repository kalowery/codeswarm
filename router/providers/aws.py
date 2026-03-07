import json
import math
import os
import shlex
import subprocess
import time
import uuid
from pathlib import Path
from pathlib import PurePosixPath
from typing import Callable, Dict, Optional

from .base import ClusterProvider


class AwsProvider(ClusterProvider):

    def __init__(self, config: dict):
        self.config = config
        self.cluster_cfg = config.get("cluster", {})
        self.aws_cfg = self.cluster_cfg.get("aws", {})

        self.region = str(self.aws_cfg.get("region") or "").strip()
        self.ami_id = str(self.aws_cfg.get("ami_id") or "").strip()
        self.subnet_id = str(self.aws_cfg.get("subnet_id") or "").strip()
        self.key_name = str(self.aws_cfg.get("key_name") or "").strip()
        self.ssh_user = str(self.aws_cfg.get("ssh_user") or "ubuntu").strip()
        raw_key_path = str(self.aws_cfg.get("ssh_private_key_path") or "").strip()
        self.ssh_private_key_path = str(Path(raw_key_path).expanduser()) if raw_key_path else ""
        self.workspace_root = str(self.cluster_cfg.get("workspace_root") or "").rstrip("/")
        self.cluster_subdir = str(self.cluster_cfg.get("cluster_subdir") or "").strip("/")
        self.base_path = f"{self.workspace_root}/{self.cluster_subdir}"

        if not self.region:
            raise RuntimeError("Missing AWS region in cluster.aws.region")

        self.state_file = Path(__file__).resolve().parents[1] / "aws_provider_state.json"
        self._state_cache = self._load_state()

    def _load_state(self) -> dict:
        try:
            if not self.state_file.exists():
                return {"jobs": {}}
            data = json.loads(self.state_file.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                return {"jobs": {}}
            jobs = data.get("jobs")
            if not isinstance(jobs, dict):
                data["jobs"] = {}
            return data
        except Exception:
            return {"jobs": {}}

    def _save_state(self) -> None:
        self.state_file.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.state_file.with_suffix(".tmp")
        tmp.write_text(json.dumps(self._state_cache, indent=2), encoding="utf-8")
        tmp.replace(self.state_file)

    def _get_job_meta(self, job_id: str) -> dict | None:
        jobs = self._state_cache.get("jobs")
        if not isinstance(jobs, dict):
            return None
        meta = jobs.get(str(job_id))
        return meta if isinstance(meta, dict) else None

    def _set_job_meta(self, job_id: str, meta: dict) -> None:
        jobs = self._state_cache.setdefault("jobs", {})
        jobs[str(job_id)] = meta
        self._save_state()

    def _delete_job_meta(self, job_id: str) -> None:
        jobs = self._state_cache.setdefault("jobs", {})
        jobs.pop(str(job_id), None)
        self._save_state()

    def _aws(self, args: list[str], expect_json: bool = False):
        cmd = ["aws", "--region", self.region] + args
        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode != 0:
            auth_error = self._aws_auth_error(result.stdout, result.stderr)
            if auth_error:
                raise RuntimeError(auth_error)
            raise RuntimeError(
                f"AWS command failed: {' '.join(cmd)}\n"
                f"STDOUT:\n{result.stdout}\nSTDERR:\n{result.stderr}"
            )
        if expect_json:
            out = result.stdout.strip() or "{}"
            try:
                return json.loads(out)
            except json.JSONDecodeError as e:
                raise RuntimeError(f"Failed to parse AWS JSON output: {e}\nOutput: {out}") from e
        return result

    @staticmethod
    def _aws_auth_error(stdout: str, stderr: str) -> str | None:
        combined = f"{stdout}\n{stderr}".lower()
        markers = [
            "session has expired",
            "expiredtoken",
            "invalidclienttokenid",
            "unable to locate credentials",
            "could not be found",
            "no credential providers",
            "the security token included in the request is invalid",
        ]
        if any(marker in combined for marker in markers):
            return (
                "AWS authentication on this launch host is invalid or expired. "
                "Run `aws login` (or refresh your AWS credentials/profile), confirm with "
                "`aws sts get-caller-identity`, then retry launch."
            )
        return None

    def _verify_aws_auth(self) -> None:
        # Fail fast before provisioning resources if local AWS credentials are stale.
        self._aws(["sts", "get-caller-identity"], expect_json=True)

    def _ssh_cmd(self, host: str, remote_cmd: str) -> list[str]:
        if not self.ssh_private_key_path:
            raise RuntimeError("Missing AWS SSH key in cluster.aws.ssh_private_key_path")
        if not self.ssh_user:
            raise RuntimeError("Missing AWS SSH user in cluster.aws.ssh_user")

        return [
            "ssh",
            "-i",
            self.ssh_private_key_path,
            "-o",
            "BatchMode=yes",
            "-o",
            "ConnectTimeout=10",
            "-o",
            "StrictHostKeyChecking=no",
            "-o",
            "UserKnownHostsFile=/dev/null",
            f"{self.ssh_user}@{host}",
            remote_cmd,
        ]

    def _ssh(self, host: str, remote_cmd: str, input_text: str | None = None) -> subprocess.CompletedProcess:
        return subprocess.run(
            self._ssh_cmd(host, remote_cmd),
            input=input_text,
            capture_output=True,
            text=True,
        )

    def _local_openai_api_key(self) -> str:
        key = str(os.environ.get("OPENAI_API_KEY") or "").strip()
        if not key:
            raise RuntimeError(
                "OPENAI_API_KEY must be set on the machine running Codeswarm "
                "to authenticate Codex on remote AWS hosts"
            )
        return key

    def _ssh_with_openai_api_key(self, host: str, remote_script: str) -> subprocess.CompletedProcess:
        key = self._local_openai_api_key()
        wrapped = (
            "read -r OPENAI_API_KEY; "
            "export OPENAI_API_KEY; "
            "/bin/bash -lc " + self._quote(remote_script)
        )
        return self._ssh(host, wrapped, input_text=key + "\n")

    def _wait_for_ssh(self, host: str, timeout_s: int = 300) -> None:
        deadline = time.time() + timeout_s
        last_err = ""
        while time.time() < deadline:
            result = self._ssh(host, "echo ready")
            if result.returncode == 0:
                return
            last_err = (result.stderr or result.stdout or "").strip()
            time.sleep(5)
        raise RuntimeError(f"Timed out waiting for SSH on host {host}: {last_err}")

    @staticmethod
    def _quote(value: str) -> str:
        return shlex.quote(str(value))

    def _describe_instances(self, filters: list[dict] | None = None) -> list[dict]:
        args = ["ec2", "describe-instances"]
        if filters:
            args += ["--filters", json.dumps(filters)]
        payload = self._aws(args, expect_json=True)
        instances = []
        for reservation in payload.get("Reservations", []):
            if not isinstance(reservation, dict):
                continue
            for instance in reservation.get("Instances", []):
                if isinstance(instance, dict):
                    instances.append(instance)
        return instances

    def _get_instances_for_job(self, job_id: str) -> list[dict]:
        return self._describe_instances(filters=[
            {"Name": "tag:codeswarm:backend", "Values": ["aws"]},
            {"Name": "tag:codeswarm:job_id", "Values": [str(job_id)]},
            {"Name": "instance-state-name", "Values": ["pending", "running", "stopping", "stopped", "shutting-down"]},
        ])

    def _wait_instances_state(self, instance_ids: list[str], target_state: str, timeout_s: int = 600) -> None:
        if not instance_ids:
            return
        waiter = "instance-running" if target_state == "running" else "instance-terminated"
        self._aws(["ec2", "wait", waiter, "--instance-ids", *instance_ids])

    def _instance_map(self, instances: list[dict]) -> dict[str, dict]:
        return {
            str(inst.get("InstanceId")): inst
            for inst in instances
            if inst.get("InstanceId")
        }

    def _instance_tags(self, instance: dict) -> dict[str, str]:
        tags = {}
        for tag in instance.get("Tags", []):
            if not isinstance(tag, dict):
                continue
            key = tag.get("Key")
            value = tag.get("Value")
            if isinstance(key, str) and isinstance(value, str):
                tags[key] = value
        return tags

    def _preferred_host(self, instance: dict) -> str:
        use_private = bool(self.aws_cfg.get("ssh_use_private_ip", False))
        private_ip = str(instance.get("PrivateIpAddress") or "").strip()
        public_ip = str(instance.get("PublicIpAddress") or "").strip()
        if use_private and private_ip:
            return private_ip
        if public_ip:
            return public_ip
        if private_ip:
            return private_ip
        raise RuntimeError(f"Instance {instance.get('InstanceId')} has no reachable IP")

    def _run_instances(self, count: int, role: str, instance_type: str, job_id: str) -> list[str]:
        if count <= 0:
            return []

        if not self.ami_id:
            raise RuntimeError("Missing AWS AMI in cluster.aws.ami_id")
        if not self.subnet_id:
            raise RuntimeError("Missing AWS subnet in cluster.aws.subnet_id")
        if not self.key_name:
            raise RuntimeError("Missing AWS EC2 key pair in cluster.aws.key_name")

        security_groups = self.aws_cfg.get("security_group_ids")
        if isinstance(security_groups, str):
            security_group_ids = [security_groups]
        elif isinstance(security_groups, list):
            security_group_ids = [str(v) for v in security_groups if str(v).strip()]
        else:
            security_group_ids = []

        if not security_group_ids:
            single_sg = str(self.aws_cfg.get("security_group_id") or "").strip()
            if single_sg:
                security_group_ids = [single_sg]

        if not security_group_ids:
            raise RuntimeError("Missing AWS security group in cluster.aws.security_group_id(s)")

        tags = [
            {"Key": "Name", "Value": f"codeswarm-{job_id}-{role}"},
            {"Key": "codeswarm:backend", "Value": "aws"},
            {"Key": "codeswarm:job_id", "Value": job_id},
            {"Key": "codeswarm:role", "Value": role},
        ]

        extra_tags = self.aws_cfg.get("tags")
        if isinstance(extra_tags, dict):
            for key, value in extra_tags.items():
                if isinstance(key, str) and isinstance(value, (str, int, float, bool)):
                    tags.append({"Key": key, "Value": str(value)})

        args = [
            "ec2",
            "run-instances",
            "--image-id",
            self.ami_id,
            "--instance-type",
            instance_type,
            "--count",
            str(count),
            "--subnet-id",
            self.subnet_id,
            "--key-name",
            self.key_name,
            "--security-group-ids",
            *security_group_ids,
            "--tag-specifications",
            json.dumps([{"ResourceType": "instance", "Tags": tags}]),
        ]

        profile_arn = str(self.aws_cfg.get("iam_instance_profile_arn") or "").strip()
        profile_name = str(self.aws_cfg.get("iam_instance_profile_name") or "").strip()
        if profile_arn:
            args += ["--iam-instance-profile", f"Arn={profile_arn}"]
        elif profile_name:
            args += ["--iam-instance-profile", f"Name={profile_name}"]

        user_data = self._cloud_init_user_data(role)
        if user_data.strip():
            args += ["--user-data", user_data]

        payload = self._aws(args, expect_json=True)
        instances = payload.get("Instances") or []
        ids = []
        for inst in instances:
            if isinstance(inst, dict) and inst.get("InstanceId"):
                ids.append(str(inst.get("InstanceId")))

        if len(ids) != count:
            raise RuntimeError(f"Expected {count} instances for role {role}, launched {len(ids)}")

        return ids

    def _cloud_init_user_data(self, role: str) -> str:
        # Keep bootstrap minimal and distro-agnostic. Detailed setup runs over SSH after launch.
        lines = [
            "#!/bin/bash",
            "set -euxo pipefail",
            "if command -v apt-get >/dev/null 2>&1; then",
            "  export DEBIAN_FRONTEND=noninteractive",
            "  apt-get update -y",
            "  apt-get install -y python3 rsync curl xz-utils jq nfs-common",
            "  if [ \"" + role + "\" = \"coordinator\" ]; then",
            "    apt-get install -y nfs-kernel-server",
            "  fi",
            "elif command -v dnf >/dev/null 2>&1; then",
            "  dnf install -y python3 rsync curl xz jq nfs-utils",
            "elif command -v yum >/dev/null 2>&1; then",
            "  yum install -y python3 rsync curl xz jq nfs-utils",
            "fi",
            f"mkdir -p {self._quote(self.workspace_root)}",
        ]
        return "\n".join(lines)

    def _setup_shared_ebs(self, coordinator_host: str, coordinator_private_ip: str, worker_hosts: list[str], device_name: str, volume_id: str, job_id: str) -> None:
        workspace_q = self._quote(self.workspace_root)
        base_q = self._quote(self.base_path)
        device_q = self._quote(device_name)

        coordinator_script = f"""
set -euo pipefail
sudo mkdir -p {workspace_q}

if ! command -v exportfs >/dev/null 2>&1; then
  if command -v apt-get >/dev/null 2>&1; then
    export DEBIAN_FRONTEND=noninteractive

    for _ in $(seq 1 60); do
      if sudo fuser /var/lib/dpkg/lock-frontend /var/lib/apt/lists/lock /var/cache/apt/archives/lock >/dev/null 2>&1; then
        sleep 5
      else
        break
      fi
    done

    for _ in $(seq 1 3); do
      if sudo apt-get update -y && sudo apt-get install -y nfs-kernel-server nfs-common; then
        break
      fi
      sleep 5
    done
  elif command -v dnf >/dev/null 2>&1; then
    sudo dnf install -y nfs-utils
  elif command -v yum >/dev/null 2>&1; then
    sudo yum install -y nfs-utils
  fi
fi

if ! command -v exportfs >/dev/null 2>&1; then
  echo "exportfs is unavailable after package install attempts" >&2
  exit 1
fi

ROOT_SOURCE=$(findmnt -n -o SOURCE / || true)
ROOT_PKNAME=""
if [ -n "$ROOT_SOURCE" ]; then
  ROOT_PKNAME=$(lsblk -no PKNAME "$ROOT_SOURCE" 2>/dev/null || true)
fi
ROOT_DISK=""
if [ -n "$ROOT_PKNAME" ]; then
  ROOT_DISK="/dev/$ROOT_PKNAME"
fi

TARGET_DEVICE=""
for dev in $(lsblk -dn -o NAME,TYPE | awk '$2=="disk" {{print "/dev/"$1}}'); do
  case "$dev" in
    /dev/loop*|/dev/ram*)
      continue
      ;;
  esac
  if [ -n "$ROOT_DISK" ] && [ "$dev" = "$ROOT_DISK" ]; then
    continue
  fi
  if lsblk -n -o MOUNTPOINT "$dev" | grep -qE '[^[:space:]]'; then
    continue
  fi
  TARGET_DEVICE="$dev"
  break
done

if [ -z "$TARGET_DEVICE" ] && [ -b {device_q} ]; then
  TARGET_DEVICE={device_q}
fi

if [ -z "$TARGET_DEVICE" ]; then
  echo "Unable to resolve attached EBS device for volume {volume_id}" >&2
  lsblk -o NAME,KNAME,TYPE,SIZE,MOUNTPOINT >&2 || true
  exit 1
fi

if ! sudo blkid "$TARGET_DEVICE" >/dev/null 2>&1; then
  sudo mkfs.ext4 -F "$TARGET_DEVICE"
fi
if ! mountpoint -q {workspace_q}; then
  sudo mount "$TARGET_DEVICE" {workspace_q}
fi
sudo chown -R $USER:$USER {workspace_q}
mkdir -p {base_q}/runs {base_q}/mailbox/inbox {base_q}/mailbox/outbox {base_q}/tools
EXPORT_FILE=/etc/exports.d/codeswarm-{job_id}.exports
if [ ! -d /etc/exports.d ]; then
  sudo mkdir -p /etc/exports.d
fi
if [ -d /etc/exports.d ]; then
  echo "{self.workspace_root} *(rw,sync,no_subtree_check,no_root_squash)" | sudo tee "$EXPORT_FILE" >/dev/null
else
  echo "{self.workspace_root} *(rw,sync,no_subtree_check,no_root_squash)" | sudo tee -a /etc/exports >/dev/null
fi
sudo exportfs -ra
if command -v systemctl >/dev/null 2>&1; then
  sudo systemctl enable --now nfs-server || sudo systemctl enable --now nfs-kernel-server || true
  sudo systemctl restart nfs-server || sudo systemctl restart nfs-kernel-server || true
fi
"""
        res = self._ssh(coordinator_host, "/bin/bash -lc " + self._quote(coordinator_script))
        if res.returncode != 0:
            raise RuntimeError(f"Failed to configure coordinator shared EBS mount:\n{res.stderr}")

        worker_script = f"""
set -euo pipefail
sudo mkdir -p {workspace_q}
if mountpoint -q {workspace_q}; then
  exit 0
fi
sudo mount -t nfs -o rw,nfsvers=4.1 {coordinator_private_ip}:{self.workspace_root} {workspace_q} \
  || sudo mount -t nfs {coordinator_private_ip}:{self.workspace_root} {workspace_q}
"""
        for host in worker_hosts:
            res = self._ssh(host, "/bin/bash -lc " + self._quote(worker_script))
            if res.returncode != 0:
                raise RuntimeError(f"Failed to mount shared workspace on worker {host}:\n{res.stderr}")

    def _sync_agent_dir(self, coordinator_host: str) -> None:
        agent_local_dir = Path(__file__).resolve().parents[2] / "agent"
        if not agent_local_dir.exists():
            raise RuntimeError(f"Local agent directory not found: {agent_local_dir}")

        ssh_base = " ".join([
            "ssh",
            "-i",
            shlex.quote(self.ssh_private_key_path),
            "-o",
            "StrictHostKeyChecking=no",
            "-o",
            "UserKnownHostsFile=/dev/null",
        ])
        remote_path = f"{self.ssh_user}@{coordinator_host}:{self.base_path}/agent/"

        mkdir_res = self._ssh(coordinator_host, f"mkdir -p {self._quote(self.base_path + '/agent')}")
        if mkdir_res.returncode != 0:
            raise RuntimeError(f"Failed to prepare remote agent directory:\n{mkdir_res.stderr}")

        subprocess.run(
            [
                "rsync",
                "-az",
                "-e",
                ssh_base,
                str(agent_local_dir) + "/",
                remote_path,
            ],
            check=True,
        )

    def _ensure_codex_tools(self, coordinator_host: str) -> None:
        node_version = str(self.aws_cfg.get("node_version") or "24.13.0")
        codex_version = str(self.aws_cfg.get("codex_version") or "latest")
        beads_version = str(self.aws_cfg.get("beads_version") or "latest")

        script = f"""
set -euo pipefail
TOOLS_DIR={self._quote(self.base_path + '/tools')}
NODE_DIR="$TOOLS_DIR/node"
NPM_BIN="$NODE_DIR/bin/npm"
NPM_PREFIX="$TOOLS_DIR/npm-global"
CODEX_BIN="$NPM_PREFIX/bin/codex"
BEADS_BIN="$NPM_PREFIX/bin/beads"
export PATH="$NODE_DIR/bin:$NPM_PREFIX/bin:$PATH"

mkdir -p "$TOOLS_DIR" "$NPM_PREFIX"

if [ ! -x "$NPM_BIN" ]; then
  ARCH=$(uname -m)
  if [ "$ARCH" = "x86_64" ]; then
    NODE_ARCH="x64"
  elif [ "$ARCH" = "aarch64" ] || [ "$ARCH" = "arm64" ]; then
    NODE_ARCH="arm64"
  else
    echo "Unsupported architecture: $ARCH" >&2
    exit 1
  fi
  mkdir -p "$NODE_DIR"
  TMP_DIR=$(mktemp -d)
  curl -fsSL "https://nodejs.org/dist/v{node_version}/node-v{node_version}-linux-$NODE_ARCH.tar.xz" -o "$TMP_DIR/node.tar.xz"
  tar -xJf "$TMP_DIR/node.tar.xz" -C "$TMP_DIR"
  SRC_DIR=$(find "$TMP_DIR" -maxdepth 1 -type d -name "node-v{node_version}-linux-*")
  cp -a "$SRC_DIR"/. "$NODE_DIR"/
  rm -rf "$TMP_DIR"
fi

NPM_CONFIG_PREFIX="$NPM_PREFIX" "$NPM_BIN" install -g @openai/codex@{codex_version}
NPM_CONFIG_PREFIX="$NPM_PREFIX" "$NPM_BIN" install -g beads@{beads_version} || true

"$CODEX_BIN" --version >/dev/null
if [ -x "$BEADS_BIN" ]; then
  "$BEADS_BIN" --version >/dev/null
fi

if ! "$CODEX_BIN" login status >/dev/null 2>&1; then
  if [ -z "${{OPENAI_API_KEY:-}}" ]; then
    echo "OPENAI_API_KEY is required to initialize Codex login" >&2
    exit 1
  fi
  if ! printenv OPENAI_API_KEY | "$CODEX_BIN" login --using-api-key; then
    printenv OPENAI_API_KEY | "$CODEX_BIN" login --with-api-key
  fi
fi
"""
        res = self._ssh_with_openai_api_key(coordinator_host, script)
        if res.returncode != 0:
            raise RuntimeError(f"Failed to bootstrap Codex tooling on coordinator:\n{res.stderr}")

    def _prepare_run_directories(
        self,
        coordinator_host: str,
        job_id: str,
        workers: int,
        agents_md_content: str | None,
        agents_bundle: dict | None,
    ) -> None:
        run_base = f"{self.base_path}/runs/{job_id}"
        res = self._ssh(coordinator_host, f"mkdir -p {self._quote(run_base)}")
        if res.returncode != 0:
            raise RuntimeError(f"Failed to create run root directory:\n{res.stderr}")

        bundle_md = (agents_bundle or {}).get("agents_md_content") if isinstance(agents_bundle, dict) else None
        effective_md = bundle_md if isinstance(bundle_md, str) and bundle_md.strip() else agents_md_content
        skill_files = self._bundle_skills_files(agents_bundle)

        for i in range(workers):
            agent_dir = f"{run_base}/agent_{i:02d}"
            cmd = f"mkdir -p {self._quote(agent_dir)} && echo {self._quote(f'Agent {i}: Say hello in one short sentence.')} > {self._quote(agent_dir + '/PROMPT.txt')}"
            res = self._ssh(coordinator_host, cmd)
            if res.returncode != 0:
                raise RuntimeError(f"Failed to initialize workspace for worker {i}:\n{res.stderr}")
            if isinstance(effective_md, str) and effective_md.strip():
                res = self._ssh(
                    coordinator_host,
                    f"cat > {self._quote(agent_dir + '/AGENTS.md')}",
                    input_text=effective_md,
                )
                if res.returncode != 0:
                    raise RuntimeError(f"Failed to write AGENTS.md for worker {i}:\n{res.stderr}")
            for rel_path, content in skill_files:
                remote_path = f"{agent_dir}/.agents/skills/{rel_path}"
                remote_dir = str(PurePosixPath(remote_path).parent)
                res = self._ssh(
                    coordinator_host,
                    f"mkdir -p {self._quote(remote_dir)} && cat > {self._quote(remote_path)}",
                    input_text=content,
                )
                if res.returncode != 0:
                    raise RuntimeError(f"Failed to write .agents/skills/{rel_path} for worker {i}:\n{res.stderr}")

    def _start_workers(self, host_assignments: dict[str, list[int]], job_id: str) -> None:
        for host, worker_ids in host_assignments.items():
            if not worker_ids:
                continue
            worker_list = " ".join(str(i) for i in worker_ids)
            script = f"""
set -euo pipefail
BASE={self._quote(self.base_path)}
JOB={self._quote(job_id)}
NODE_DIR="$BASE/tools/node"
NPM_PREFIX="$BASE/tools/npm-global"
CODEX_BIN="$NPM_PREFIX/bin/codex"
export PATH="$NODE_DIR/bin:$NPM_PREFIX/bin:$PATH"

if ! "$CODEX_BIN" login status >/dev/null 2>&1; then
  if [ -z "${{OPENAI_API_KEY:-}}" ]; then
    echo "OPENAI_API_KEY is required on each compute node for Codex login" >&2
    exit 1
  fi

  # Support both CLI flag spellings across codex versions.
  if ! printenv OPENAI_API_KEY | "$CODEX_BIN" login --using-api-key; then
    printenv OPENAI_API_KEY | "$CODEX_BIN" login --with-api-key
  fi
fi

for wid in {worker_list}; do
  AGENT_INDEX=$(printf "%02d" "$wid")
  AGENT_WORKDIR="$BASE/runs/$JOB/agent_$AGENT_INDEX"
  mkdir -p "$AGENT_WORKDIR"
  (
    cd "$AGENT_WORKDIR"
    export CODESWARM_JOB_ID="$JOB"
    export CODESWARM_NODE_ID="$wid"
    export CODESWARM_BASE_DIR="$BASE"
    export CODESWARM_CODEX_BIN="$BASE/tools/npm-global/bin/codex"
    export PATH="$BASE/tools/node/bin:$BASE/tools/npm-global/bin:$PATH"
    nohup python3 "$BASE/agent/codex_worker.py" >> "$AGENT_WORKDIR/worker.log" 2>&1 &
    echo $! > "$AGENT_WORKDIR/worker.pid"
  )
done
"""
            res = self._ssh_with_openai_api_key(host, script)
            if res.returncode != 0:
                raise RuntimeError(f"Failed to launch workers on host {host}:\n{res.stderr}")

    def launch(
        self,
        nodes: int,
        agents_md_content: str | None = None,
        agents_bundle: dict | None = None,
        launch_params: dict | None = None,
        progress_cb: Callable[[str, str], None] | None = None,
    ) -> str:
        def _progress(stage: str, message: str):
            if callable(progress_cb):
                try:
                    progress_cb(stage, message)
                except Exception:
                    pass

        launch_params = launch_params if isinstance(launch_params, dict) else {}
        total_workers = int(nodes)
        if total_workers < 1:
            raise RuntimeError("Swarm launch requires at least one agent")

        _progress("starting", f"Preparing AWS launch for {total_workers} worker(s)")
        _progress("auth", "Validating AWS CLI authentication on launch host")
        self._verify_aws_auth()

        instance_type = str(launch_params.get("instance_type") or self.aws_cfg.get("instance_type") or "").strip()
        if not instance_type:
            raise RuntimeError("AWS launch requires instance_type")
        _progress("config", f"Using instance type: {instance_type}")

        workers_per_node = int(launch_params.get("workers_per_node") or self.aws_cfg.get("workers_per_node") or 1)
        if workers_per_node < 1:
            raise RuntimeError("workers_per_node must be >= 1")

        requested_node_count = launch_params.get("node_count")
        if requested_node_count is None:
            compute_nodes = int(math.ceil(total_workers / workers_per_node))
        else:
            compute_nodes = int(requested_node_count)
        if compute_nodes < 1:
            raise RuntimeError("node_count must be >= 1")
        if compute_nodes * workers_per_node < total_workers:
            raise RuntimeError(
                f"Insufficient capacity: node_count({compute_nodes}) * workers_per_node({workers_per_node}) < agents({total_workers})"
            )
        _progress("config", f"Compute nodes: {compute_nodes} - workers per node: {workers_per_node}")

        ebs_size_gb = int(launch_params.get("ebs_volume_size_gb") or self.aws_cfg.get("ebs_volume_size_gb") or 100)
        if ebs_size_gb < 8:
            raise RuntimeError("ebs_volume_size_gb must be at least 8")
        _progress("config", f"Shared EBS size: {ebs_size_gb} GiB")

        volume_type = str(launch_params.get("ebs_volume_type") or self.aws_cfg.get("ebs_volume_type") or "gp3")
        delete_on_shutdown = bool(launch_params.get("delete_ebs_on_shutdown", False))
        ebs_device = str(self.aws_cfg.get("ebs_device_name") or "/dev/sdf")

        job_id = f"aws_{uuid.uuid4().hex[:12]}"
        volume_id = None
        launched_instance_ids: list[str] = []

        try:
            _progress("provision", "Launching coordinator and worker EC2 instances")
            coordinator_ids = self._run_instances(1, "coordinator", instance_type, job_id)
            worker_ids = self._run_instances(max(0, compute_nodes - 1), "worker", instance_type, job_id)
            launched_instance_ids = coordinator_ids + worker_ids

            _progress("provision", "Waiting for EC2 instances to reach running state")
            self._wait_instances_state(launched_instance_ids, "running")

            all_instances = self._get_instances_for_job(job_id)
            instances_by_id = self._instance_map(all_instances)
            coordinator = instances_by_id.get(coordinator_ids[0])
            if not coordinator:
                raise RuntimeError("Unable to resolve coordinator instance metadata")

            availability_zone = str(
                self.aws_cfg.get("availability_zone")
                or (coordinator.get("Placement", {}) or {}).get("AvailabilityZone")
                or ""
            ).strip()
            if not availability_zone:
                raise RuntimeError("Unable to determine availability zone for EBS volume")
            _progress("storage", f"Creating shared EBS volume in {availability_zone}")

            vol_tags = [
                {"Key": "Name", "Value": f"codeswarm-{job_id}-shared"},
                {"Key": "codeswarm:backend", "Value": "aws"},
                {"Key": "codeswarm:job_id", "Value": job_id},
            ]
            create_vol_args = [
                "ec2",
                "create-volume",
                "--availability-zone",
                availability_zone,
                "--size",
                str(ebs_size_gb),
                "--volume-type",
                volume_type,
                "--tag-specifications",
                json.dumps([{"ResourceType": "volume", "Tags": vol_tags}]),
            ]
            if volume_type == "gp3":
                if launch_params.get("ebs_iops") is not None:
                    create_vol_args += ["--iops", str(int(launch_params.get("ebs_iops")))]
                if launch_params.get("ebs_throughput") is not None:
                    create_vol_args += ["--throughput", str(int(launch_params.get("ebs_throughput")))]

            vol = self._aws(create_vol_args, expect_json=True)
            volume_id = str(vol.get("VolumeId") or "").strip()
            if not volume_id:
                raise RuntimeError("Failed to parse created EBS volume id")

            self._aws(["ec2", "wait", "volume-available", "--volume-ids", volume_id])
            _progress("storage", f"Attaching EBS volume {volume_id} to coordinator")
            self._aws([
                "ec2",
                "attach-volume",
                "--volume-id",
                volume_id,
                "--instance-id",
                coordinator_ids[0],
                "--device",
                ebs_device,
            ])
            self._aws(["ec2", "wait", "volume-in-use", "--volume-ids", volume_id])

            coordinator_host = self._preferred_host(coordinator)
            coordinator_private_ip = str(coordinator.get("PrivateIpAddress") or "").strip()
            if not coordinator_private_ip:
                raise RuntimeError("Coordinator private IP is required for NFS mounting")

            worker_hosts = []
            for instance_id in worker_ids:
                inst = instances_by_id.get(instance_id)
                if not inst:
                    continue
                worker_hosts.append(self._preferred_host(inst))

            for host in [coordinator_host] + worker_hosts:
                _progress("bootstrap", f"Waiting for SSH on {host}")
                self._wait_for_ssh(host)

            _progress("bootstrap", "Configuring shared workspace and NFS mount")
            self._setup_shared_ebs(
                coordinator_host=coordinator_host,
                coordinator_private_ip=coordinator_private_ip,
                worker_hosts=worker_hosts,
                device_name=ebs_device,
                volume_id=volume_id,
                job_id=job_id,
            )
            _progress("bootstrap", "Syncing agent runtime files")
            self._sync_agent_dir(coordinator_host)
            _progress("bootstrap", "Installing Codex runtime tools")
            self._ensure_codex_tools(coordinator_host)
            _progress("bootstrap", "Preparing per-worker run directories")
            self._prepare_run_directories(
                coordinator_host,
                job_id,
                total_workers,
                agents_md_content,
                agents_bundle,
            )

            host_order = [coordinator_host] + worker_hosts
            assignments: dict[str, list[int]] = {host: [] for host in host_order}
            worker_mapping = {}
            for worker_id in range(total_workers):
                host_index = worker_id // workers_per_node
                if host_index >= len(host_order):
                    raise RuntimeError(f"No compute node slot available for worker {worker_id}")
                host = host_order[host_index]
                assignments[host].append(worker_id)
                worker_mapping[str(worker_id)] = {
                    "host": host,
                    "instance_id": launched_instance_ids[host_index],
                    "worker_slot": worker_id % workers_per_node,
                }

            _progress("bootstrap", "Launching worker processes")
            self._start_workers(assignments, job_id)

            self._set_job_meta(job_id, {
                "job_id": job_id,
                "created_at": int(time.time()),
                "status": "running",
                "coordinator_instance_id": coordinator_ids[0],
                "coordinator_host": coordinator_host,
                "coordinator_private_ip": coordinator_private_ip,
                "instance_ids": launched_instance_ids,
                "worker_instance_ids": worker_ids,
                "volume_id": volume_id,
                "delete_ebs_on_shutdown": delete_on_shutdown,
                "workspace_root": self.workspace_root,
                "cluster_subdir": self.cluster_subdir,
                "base_path": self.base_path,
                "total_workers": total_workers,
                "compute_nodes": compute_nodes,
                "workers_per_node": workers_per_node,
                "worker_mapping": worker_mapping,
                "ssh_user": self.ssh_user,
                "ssh_private_key_path": self.ssh_private_key_path,
            })

            _progress("ready", f"AWS swarm ready: {job_id}")

            return job_id

        except Exception:
            if launched_instance_ids:
                try:
                    self._aws(["ec2", "terminate-instances", "--instance-ids", *launched_instance_ids])
                except Exception:
                    pass
            if volume_id:
                try:
                    self._aws(["ec2", "wait", "volume-available", "--volume-ids", volume_id])
                    self._aws(["ec2", "delete-volume", "--volume-id", volume_id])
                except Exception:
                    pass
            raise

    def terminate(self, job_id: str, terminate_params: dict | None = None) -> None:
        meta = self._get_job_meta(job_id) or {}
        instances = self._get_instances_for_job(job_id)
        instance_ids = [str(i.get("InstanceId")) for i in instances if i.get("InstanceId")]
        if not instance_ids and isinstance(meta.get("instance_ids"), list):
            instance_ids = [str(i) for i in meta.get("instance_ids") if str(i).strip()]

        delete_on_shutdown = bool(meta.get("delete_ebs_on_shutdown", False))
        if isinstance(terminate_params, dict) and "delete_ebs_on_shutdown" in terminate_params:
            delete_on_shutdown = bool(terminate_params.get("delete_ebs_on_shutdown"))

        if instance_ids:
            self._aws(["ec2", "terminate-instances", "--instance-ids", *instance_ids])
            self._wait_instances_state(instance_ids, "terminated")

        volume_id = str(meta.get("volume_id") or "").strip()
        if not volume_id:
            vols = self._aws([
                "ec2",
                "describe-volumes",
                "--filters",
                json.dumps([
                    {"Name": "tag:codeswarm:backend", "Values": ["aws"]},
                    {"Name": "tag:codeswarm:job_id", "Values": [str(job_id)]},
                ]),
            ], expect_json=True)
            volumes = vols.get("Volumes") or []
            if volumes and isinstance(volumes[0], dict):
                volume_id = str(volumes[0].get("VolumeId") or "").strip()

        if delete_on_shutdown and volume_id:
            try:
                self._aws(["ec2", "wait", "volume-available", "--volume-ids", volume_id])
            except Exception:
                try:
                    self._aws(["ec2", "detach-volume", "--volume-id", volume_id, "--force"])
                    self._aws(["ec2", "wait", "volume-available", "--volume-ids", volume_id])
                except Exception:
                    pass
            try:
                self._aws(["ec2", "delete-volume", "--volume-id", volume_id])
            except Exception as e:
                raise RuntimeError(f"Failed to delete EBS volume {volume_id}: {e}") from e

        self._delete_job_meta(job_id)

    def archive(self, job_id: str, swarm_id: str) -> None:
        # AWS backend keeps data on EBS unless delete_ebs_on_shutdown is enabled.
        return

    def get_job_state(self, job_id: str) -> Optional[str]:
        instances = self._get_instances_for_job(job_id)
        if not instances:
            return None

        states = {
            str((inst.get("State") or {}).get("Name") or "").lower()
            for inst in instances
        }
        if "running" in states:
            return "RUNNING"
        if "pending" in states:
            return "PENDING"
        if "stopping" in states or "shutting-down" in states:
            return "TERMINATING"
        if "stopped" in states:
            return "STOPPED"
        return "UNKNOWN"

    def list_active_jobs(self) -> Dict[str, str]:
        instances = self._describe_instances(filters=[
            {"Name": "tag:codeswarm:backend", "Values": ["aws"]},
            {"Name": "instance-state-name", "Values": ["pending", "running", "stopping", "stopped", "shutting-down"]},
        ])

        by_job: dict[str, set[str]] = {}
        for inst in instances:
            tags = self._instance_tags(inst)
            job_id = tags.get("codeswarm:job_id")
            if not job_id:
                continue
            state = str((inst.get("State") or {}).get("Name") or "").lower()
            by_job.setdefault(job_id, set()).add(state)

        results: Dict[str, str] = {}
        for job_id, states in by_job.items():
            if "running" in states:
                results[job_id] = "RUNNING"
            elif "pending" in states:
                results[job_id] = "PENDING"
            elif "stopping" in states or "shutting-down" in states:
                results[job_id] = "TERMINATING"
            elif "stopped" in states:
                results[job_id] = "STOPPED"
            else:
                results[job_id] = "UNKNOWN"

        return results

    def start_follower(self):
        follower_path = Path(__file__).resolve().parent / "aws_follower.py"
        return subprocess.Popen(
            [
                "python3",
                "-u",
                str(follower_path),
                "--state-file",
                str(self.state_file),
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            bufsize=0,
        )

    def _coordinator_host_for_job(self, job_id: str) -> str:
        meta = self._get_job_meta(job_id) or {}
        host = str(meta.get("coordinator_host") or "").strip()
        if host:
            return host

        instances = self._describe_instances(filters=[
            {"Name": "tag:codeswarm:backend", "Values": ["aws"]},
            {"Name": "tag:codeswarm:job_id", "Values": [str(job_id)]},
            {"Name": "tag:codeswarm:role", "Values": ["coordinator"]},
            {"Name": "instance-state-name", "Values": ["pending", "running", "stopping", "stopped"]},
        ])
        if not instances:
            raise RuntimeError(f"Unable to resolve coordinator host for AWS job {job_id}")

        host = self._preferred_host(instances[0])
        if not meta:
            meta = {"job_id": str(job_id)}
        meta["coordinator_host"] = host
        self._set_job_meta(job_id, meta)
        return host

    def inject(self, job_id, node_id, content, injection_id):
        coordinator_host = self._coordinator_host_for_job(str(job_id))
        inbox_path = f"{self.base_path}/mailbox/inbox/{job_id}_{int(node_id):02d}.jsonl"
        payload = {
            "type": "user",
            "content": content,
            "injection_id": injection_id,
        }
        json_line = json.dumps(payload)
        remote_cmd = f"printf '%s\\n' {shlex.quote(json_line)} >> {shlex.quote(inbox_path)}"
        result = self._ssh(coordinator_host, remote_cmd)
        if result.returncode != 0:
            raise RuntimeError(result.stderr.strip() or result.stdout.strip())

    def send_control(self, job_id: str, node_id: int, message: dict) -> None:
        coordinator_host = self._coordinator_host_for_job(str(job_id))
        inbox_path = f"{self.base_path}/mailbox/inbox/{job_id}_{int(node_id):02d}.jsonl"
        payload = {
            "type": "control",
            "payload": message,
        }
        json_line = json.dumps(payload)
        remote_cmd = f"printf '%s\\n' {shlex.quote(json_line)} >> {shlex.quote(inbox_path)}"
        result = self._ssh(coordinator_host, remote_cmd)
        if result.returncode != 0:
            raise RuntimeError(result.stderr.strip() or result.stdout.strip())

    @staticmethod
    def _safe_skill_rel_path(path: str) -> str | None:
        try:
            parts = PurePosixPath(path).parts
        except Exception:
            return None
        if not parts:
            return None
        if any(part in ("", ".", "..") for part in parts):
            return None
        return str(PurePosixPath(*parts))

    def _bundle_skills_files(self, agents_bundle: dict | None) -> list[tuple[str, str]]:
        if not isinstance(agents_bundle, dict):
            return []
        if str(agents_bundle.get("mode") or "file") != "directory":
            return []
        raw_files = agents_bundle.get("skills_files")
        if not isinstance(raw_files, list):
            return []
        files: list[tuple[str, str]] = []
        for item in raw_files:
            if not isinstance(item, dict):
                continue
            rel_path = item.get("path")
            content = item.get("content")
            if not isinstance(rel_path, str) or not isinstance(content, str):
                continue
            safe_rel = self._safe_skill_rel_path(rel_path)
            if not safe_rel:
                continue
            files.append((safe_rel, content))
        return files
