import json
from pathlib import Path


REQUIRED_FIELDS = [
    ("ssh", "login_alias"),
    ("cluster", "workspace_root"),
    ("cluster", "cluster_subdir"),
]


def load_config(path):
    path = Path(path)
    if not path.exists():
        raise RuntimeError(f"Config file not found: {path}")

    data = json.loads(path.read_text())

    # Validate required fields
    for section, key in REQUIRED_FIELDS:
        if section not in data or key not in data[section]:
            raise RuntimeError(
                f"Missing required config field: {section}.{key}"
            )

    return data
