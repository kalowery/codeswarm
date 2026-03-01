import json
from pathlib import Path


def load_config(path):
    path = Path(path)
    if not path.exists():
        raise RuntimeError(f"Config file not found: {path}")

    data = json.loads(path.read_text())

    # Backend-specific validation
    backend = data.get("cluster", {}).get("backend", "slurm")

    if backend == "slurm":
        required = [
            ("ssh", "login_alias"),
            ("cluster", "workspace_root"),
            ("cluster", "cluster_subdir"),
        ]
    elif backend == "local":
        # Local provider has safe defaults; no required SSH fields
        required = []
    else:
        raise RuntimeError(f"Unsupported cluster backend: {backend}")

    for section, key in required:
        if section not in data or key not in data[section]:
            raise RuntimeError(
                f"Missing required config field: {section}.{key}"
            )

    return data
