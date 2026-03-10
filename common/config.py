import json
from pathlib import Path


def _cluster_section(data):
    cluster_cfg = data.get("cluster")
    return cluster_cfg if isinstance(cluster_cfg, dict) else {}


def _backend_section(data, backend):
    cluster_cfg = _cluster_section(data)
    section = cluster_cfg.get(backend)
    return section if isinstance(section, dict) else {}


def _resolve_backend_profile_config(data, backend, profile=None):
    cluster_cfg = _cluster_section(data)
    backend_cfg = _backend_section(data, backend)

    profiles = backend_cfg.get("profiles") if isinstance(backend_cfg, dict) else None
    if isinstance(profiles, dict):
        profile_name = str(profile).strip() if isinstance(profile, str) and str(profile).strip() else ""
        if not profile_name:
            if "default" in profiles and isinstance(profiles.get("default"), dict):
                profile_name = "default"
            else:
                for key, value in profiles.items():
                    if isinstance(value, dict):
                        profile_name = str(key)
                        break
        selected = profiles.get(profile_name)
        if not isinstance(selected, dict):
            raise RuntimeError(
                f"Missing or invalid cluster profile '{profile_name or profile}' for backend '{backend}'"
            )
        merged = {k: v for k, v in backend_cfg.items() if k != "profiles"}
        merged.update(selected)
        return cluster_cfg, merged, profile_name

    if isinstance(profile, str) and profile.strip():
        nested = backend_cfg.get(profile.strip())
        if isinstance(nested, dict):
            merged = {k: v for k, v in backend_cfg.items() if k != profile.strip()}
            merged.update(nested)
            return cluster_cfg, merged, profile.strip()
        raise RuntimeError(
            f"Missing cluster profile '{profile.strip()}' for backend '{backend}'"
        )

    return cluster_cfg, backend_cfg, None


def load_config(path):
    path = Path(path)
    if not path.exists():
        raise RuntimeError(f"Config file not found: {path}")

    data = json.loads(path.read_text())

    # Backend-specific validation (supports legacy single backend or launch_providers)
    backend_targets = []
    launch_providers = data.get("launch_providers")
    if isinstance(launch_providers, dict):
        for value in launch_providers.values():
            if isinstance(value, dict) and value.get("backend"):
                backend = str(value.get("backend"))
                profile = value.get("cluster_profile")
                if profile is None:
                    profile = value.get("cluster_config")
                backend_targets.append((backend, str(profile).strip() if isinstance(profile, str) and str(profile).strip() else None))
    elif isinstance(launch_providers, list):
        for value in launch_providers:
            if isinstance(value, dict) and value.get("backend"):
                backend = str(value.get("backend"))
                profile = value.get("cluster_profile")
                if profile is None:
                    profile = value.get("cluster_config")
                backend_targets.append((backend, str(profile).strip() if isinstance(profile, str) and str(profile).strip() else None))

    legacy_backend = data.get("cluster", {}).get("backend")
    if legacy_backend:
        backend_targets.append((str(legacy_backend), None))

    if not backend_targets:
        backend_targets.append(("slurm", None))

    required = []
    seen = set()
    for backend, profile in backend_targets:
        dedupe_key = (backend, profile)
        if dedupe_key in seen:
            continue
        seen.add(dedupe_key)
        cluster_cfg, backend_cfg, resolved_profile = _resolve_backend_profile_config(data, backend, profile)
        if backend == "slurm":
            has_login_host = "login_host" in backend_cfg or "login_alias" in backend_cfg
            if not has_login_host:
                profile_hint = (
                    f"cluster.slurm.profiles.{resolved_profile}.login_host"
                    if resolved_profile
                    else "cluster.slurm.login_host"
                )
                raise RuntimeError(f"Missing required config field: {profile_hint}")
            if "workspace_root" not in backend_cfg and "workspace_root" not in cluster_cfg:
                raise RuntimeError(
                    "Missing required config field: cluster.workspace_root "
                    "(or cluster.slurm.workspace_root"
                    + (f", cluster.slurm.profiles.{resolved_profile}.workspace_root" if resolved_profile else "")
                    + ")"
                )
            if "cluster_subdir" not in backend_cfg and "cluster_subdir" not in cluster_cfg:
                raise RuntimeError(
                    "Missing required config field: cluster.cluster_subdir "
                    "(or cluster.slurm.cluster_subdir"
                    + (f", cluster.slurm.profiles.{resolved_profile}.cluster_subdir" if resolved_profile else "")
                    + ")"
                )
        elif backend == "local":
            continue
        elif backend == "aws":
            if "workspace_root" not in backend_cfg and "workspace_root" not in cluster_cfg:
                raise RuntimeError(
                    "Missing required config field: cluster.workspace_root "
                    "(or cluster.aws.workspace_root"
                    + (f", cluster.aws.profiles.{resolved_profile}.workspace_root" if resolved_profile else "")
                    + ")"
                )
            if "cluster_subdir" not in backend_cfg and "cluster_subdir" not in cluster_cfg:
                raise RuntimeError(
                    "Missing required config field: cluster.cluster_subdir "
                    "(or cluster.aws.cluster_subdir"
                    + (f", cluster.aws.profiles.{resolved_profile}.cluster_subdir" if resolved_profile else "")
                    + ")"
                )
            for key in ("region", "ami_id", "subnet_id", "key_name", "ssh_private_key_path"):
                if key not in backend_cfg:
                    profile_hint = (
                        f"cluster.aws.profiles.{resolved_profile}.{key}"
                        if resolved_profile
                        else f"cluster.aws.{key}"
                    )
                    raise RuntimeError(f"Missing required config field: {profile_hint}")
        else:
            raise RuntimeError(f"Unsupported cluster backend: {backend}")

    for section, key in required:
        if section not in data or key not in data[section]:
            raise RuntimeError(f"Missing required config field: {section}.{key}")

    return data
