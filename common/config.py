import json
from pathlib import Path


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
            required.extend([
                ("ssh", "login_alias"),
                ("cluster", "workspace_root"),
                ("cluster", "cluster_subdir"),
            ])
        elif backend == "local":
            continue
        else:
            raise RuntimeError(f"Unsupported cluster backend: {backend}")

    for section, key in required:
        if section not in data or key not in data[section]:
            raise RuntimeError(
                f"Missing required config field: {section}.{key}"
            )

    return data
