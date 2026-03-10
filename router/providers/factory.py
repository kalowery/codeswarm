from .slurm import SlurmProvider
from .aws import AwsProvider
from .local import LocalProvider


def _resolve_backend_profile(cluster_cfg: dict, backend: str, profile: str | None = None):
    backend_cfg = cluster_cfg.get(backend)
    if not isinstance(backend_cfg, dict):
        return {}, None

    profiles = backend_cfg.get("profiles")
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
        profile_cfg = profiles.get(profile_name)
        if not isinstance(profile_cfg, dict):
            raise RuntimeError(
                f"Invalid cluster profile '{profile_name or profile}' for backend '{backend}'"
            )
        merged = {k: v for k, v in backend_cfg.items() if k != "profiles"}
        merged.update(profile_cfg)
        return merged, profile_name

    if isinstance(profile, str) and profile.strip():
        # Backward compatible support: allow cluster.<backend>.<profile> directly.
        nested = backend_cfg.get(profile.strip())
        if isinstance(nested, dict):
            merged = {k: v for k, v in backend_cfg.items() if k != profile.strip()}
            merged.update(nested)
            return merged, profile.strip()
        raise RuntimeError(
            f"Invalid cluster profile '{profile.strip()}' for backend '{backend}'"
        )

    return backend_cfg, None


def _default_launch_fields_for_backend(backend: str, backend_cfg: dict):
    if backend == "slurm":
        slurm_cfg = backend_cfg if isinstance(backend_cfg, dict) else {}
        return [
            {
                "key": "partition",
                "label": "Partition",
                "type": "text",
                "default": slurm_cfg.get("partition", ""),
                "required": False,
            },
            {
                "key": "time_limit",
                "label": "Time Limit",
                "type": "text",
                "default": slurm_cfg.get("time_limit", ""),
                "required": False,
                "placeholder": "HH:MM:SS",
            },
            {
                "key": "account",
                "label": "Account",
                "type": "text",
                "default": slurm_cfg.get("account") or "",
                "required": False,
            },
            {
                "key": "qos",
                "label": "QoS",
                "type": "text",
                "default": slurm_cfg.get("qos") or "",
                "required": False,
            },
        ]

    if backend == "aws":
        aws_cfg = backend_cfg if isinstance(backend_cfg, dict) else {}
        return [
            {
                "key": "instance_type",
                "label": "Instance Type",
                "type": "text",
                "default": aws_cfg.get("instance_type", ""),
                "required": True,
                "placeholder": "c7i.4xlarge",
            },
            {
                "key": "node_count",
                "label": "Compute Nodes",
                "type": "number",
                "default": aws_cfg.get("node_count", 1),
                "required": False,
            },
            {
                "key": "workers_per_node",
                "label": "Workers Per Node",
                "type": "number",
                "default": aws_cfg.get("workers_per_node", 1),
                "required": False,
            },
            {
                "key": "ebs_volume_size_gb",
                "label": "Shared EBS Size (GiB)",
                "type": "number",
                "default": aws_cfg.get("ebs_volume_size_gb", 100),
                "required": True,
            },
            {
                "key": "delete_ebs_on_shutdown",
                "label": "Delete EBS On Shutdown",
                "type": "boolean",
                "default": aws_cfg.get("delete_ebs_on_shutdown", False),
                "required": False,
            },
        ]

    return []


def get_provider_specs(config: dict):
    specs = []
    launch_providers = config.get("launch_providers")
    cluster_cfg = config.get("cluster", {})
    cluster_cfg = cluster_cfg if isinstance(cluster_cfg, dict) else {}

    if isinstance(launch_providers, dict):
        for provider_id, value in launch_providers.items():
            if not isinstance(value, dict):
                continue
            backend = value.get("backend")
            if not backend:
                continue
            cluster_profile = value.get("cluster_profile")
            if cluster_profile is None:
                cluster_profile = value.get("cluster_config")
            specs.append(
                {
                    "id": str(provider_id),
                    "label": value.get("label") or str(provider_id),
                    "backend": str(backend),
                    "cluster_profile": str(cluster_profile).strip() if isinstance(cluster_profile, str) else None,
                    "defaults": value.get("defaults", {}),
                    "launch_fields": value.get("launch_fields"),
                    "launch_panels": value.get("launch_panels"),
                }
            )
    elif isinstance(launch_providers, list):
        for idx, value in enumerate(launch_providers):
            if not isinstance(value, dict):
                continue
            backend = value.get("backend")
            if not backend:
                continue
            provider_id = value.get("id") or f"{backend}-{idx + 1}"
            cluster_profile = value.get("cluster_profile")
            if cluster_profile is None:
                cluster_profile = value.get("cluster_config")
            specs.append(
                {
                    "id": str(provider_id),
                    "label": value.get("label") or str(provider_id),
                    "backend": str(backend),
                    "cluster_profile": str(cluster_profile).strip() if isinstance(cluster_profile, str) else None,
                    "defaults": value.get("defaults", {}),
                    "launch_fields": value.get("launch_fields"),
                    "launch_panels": value.get("launch_panels"),
                }
            )

    if not specs:
        cluster_cfg = config.get("cluster", {})
        backend = cluster_cfg.get("backend", "slurm")
        specs = [
            {
                "id": str(backend),
                "label": str(backend).upper(),
                "backend": str(backend),
                "cluster_profile": None,
                "defaults": {},
                "launch_fields": None,
            }
        ]

    normalized = []
    seen = set()
    for spec in specs:
        provider_id = str(spec.get("id", "")).strip()
        backend = str(spec.get("backend", "")).strip()
        if not provider_id or provider_id in seen or not backend:
            continue
        seen.add(provider_id)
        cluster_profile = spec.get("cluster_profile")
        cluster_profile = (
            str(cluster_profile).strip()
            if isinstance(cluster_profile, str) and str(cluster_profile).strip()
            else None
        )
        backend_cfg, resolved_profile = _resolve_backend_profile(cluster_cfg, backend, cluster_profile)
        provider_ref = f"{backend}:{resolved_profile}" if resolved_profile else backend
        launch_fields = spec.get("launch_fields")
        if not isinstance(launch_fields, list):
            launch_fields = _default_launch_fields_for_backend(backend, backend_cfg)
        launch_panels = spec.get("launch_panels")
        if not isinstance(launch_panels, list):
            launch_panels = []
        defaults = spec.get("defaults")
        if not isinstance(defaults, dict):
            defaults = {}
        normalized.append(
            {
                "id": provider_id,
                "label": str(spec.get("label") or provider_id),
                "backend": backend,
                "provider_ref": provider_ref,
                "cluster_profile": resolved_profile,
                "defaults": defaults,
                "launch_fields": launch_fields,
                "launch_panels": launch_panels,
            }
        )
    return normalized


def _provider_config_for_backend(config: dict, backend: str, cluster_profile: str | None = None):
    base = dict(config)
    cluster_cfg = config.get("cluster", {})
    cluster_cfg = cluster_cfg if isinstance(cluster_cfg, dict) else {}

    backend_cfg, resolved_profile = _resolve_backend_profile(cluster_cfg, backend, cluster_profile)
    merged_cluster = dict(cluster_cfg)
    merged_cluster[backend] = backend_cfg
    base["cluster"] = merged_cluster
    if resolved_profile:
        base["_cluster_profile"] = resolved_profile
    base["_provider_backend"] = backend
    if resolved_profile:
        base["_provider_ref"] = f"{backend}:{resolved_profile}"
    else:
        base["_provider_ref"] = backend

    return base


def _build_backend_provider(config: dict, backend: str, cluster_profile: str | None = None):
    provider_config = _provider_config_for_backend(config, backend, cluster_profile)

    if backend == "slurm":
        return SlurmProvider(provider_config)

    if backend == "local":
        cluster_cfg = provider_config.get("cluster", {})
        local_cfg = cluster_cfg.get("local", {})
        local_cfg = {**cluster_cfg, **local_cfg}
        return LocalProvider(local_cfg)

    if backend == "aws":
        return AwsProvider(provider_config)

    raise RuntimeError(f"Unsupported cluster backend: {backend}")


def build_providers(config: dict, provider_specs: list[dict]):
    providers = {}
    for spec in provider_specs:
        backend = str(spec.get("backend") or "").strip()
        provider_ref = str(spec.get("provider_ref") or "").strip()
        cluster_profile = spec.get("cluster_profile")
        if not backend or not provider_ref or provider_ref in providers:
            continue
        providers[provider_ref] = _build_backend_provider(config, backend, cluster_profile)
    return providers
