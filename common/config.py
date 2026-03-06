import json
from pathlib import Path


def _cluster_section(data):
    cluster_cfg = data.get("cluster")
    return cluster_cfg if isinstance(cluster_cfg, dict) else {}


def _backend_section(data, backend):
    cluster_cfg = _cluster_section(data)
    section = cluster_cfg.get(backend)
    return section if isinstance(section, dict) else {}


def _has_backend_cluster_key(data, backend, key):
    cluster_cfg = _cluster_section(data)
    backend_cfg = _backend_section(data, backend)
    return key in backend_cfg or key in cluster_cfg


def load_config(path):
    path = Path(path)
    if not path.exists():
        raise RuntimeError(f"Config file not found: {path}")

    data = json.loads(path.read_text())

    # Backend-specific validation (supports legacy single backend or launch_providers)
    backends = set()
    launch_providers = data.get("launch_providers")
    if isinstance(launch_providers, dict):
        for value in launch_providers.values():
            if isinstance(value, dict) and value.get("backend"):
                backends.add(str(value.get("backend")))
    elif isinstance(launch_providers, list):
        for value in launch_providers:
            if isinstance(value, dict) and value.get("backend"):
                backends.add(str(value.get("backend")))

    legacy_backend = data.get("cluster", {}).get("backend")
    if legacy_backend:
        backends.add(str(legacy_backend))

    if not backends:
        backends.add("slurm")

    required = []
    for backend in backends:
        if backend == "slurm":
            required.extend([("ssh", "login_alias")])
            if not _has_backend_cluster_key(data, "slurm", "workspace_root"):
                raise RuntimeError(
                    "Missing required config field: cluster.workspace_root "
                    "(or cluster.slurm.workspace_root)"
                )
            if not _has_backend_cluster_key(data, "slurm", "cluster_subdir"):
                raise RuntimeError(
                    "Missing required config field: cluster.cluster_subdir "
                    "(or cluster.slurm.cluster_subdir)"
                )
        elif backend == "local":
            continue
        elif backend == "aws":
            if not _has_backend_cluster_key(data, "aws", "workspace_root"):
                raise RuntimeError(
                    "Missing required config field: cluster.workspace_root "
                    "(or cluster.aws.workspace_root)"
                )
            if not _has_backend_cluster_key(data, "aws", "cluster_subdir"):
                raise RuntimeError(
                    "Missing required config field: cluster.cluster_subdir "
                    "(or cluster.aws.cluster_subdir)"
                )
            required.extend([
                ("cluster.aws", "region"),
                ("cluster.aws", "ami_id"),
                ("cluster.aws", "subnet_id"),
                ("cluster.aws", "key_name"),
                ("cluster.aws", "ssh_private_key_path"),
            ])
        else:
            raise RuntimeError(f"Unsupported cluster backend: {backend}")

    for section, key in required:
        if section == "cluster.aws":
            cluster_cfg = data.get("cluster") if isinstance(data.get("cluster"), dict) else {}
            aws_cfg = cluster_cfg.get("aws") if isinstance(cluster_cfg.get("aws"), dict) else {}
            if key not in aws_cfg:
                raise RuntimeError(f"Missing required config field: cluster.aws.{key}")
            continue

        if section not in data or key not in data[section]:
            raise RuntimeError(f"Missing required config field: {section}.{key}")

    return data
