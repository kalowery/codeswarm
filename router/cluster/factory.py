from .slurm import SlurmProvider
from ..providers.local_provider import LocalProvider


def _default_launch_fields_for_backend(backend: str, config: dict):
    if backend == "slurm":
        slurm_cfg = config.get("cluster", {}).get("slurm", {})
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

    return []


def get_provider_specs(config: dict):
    specs = []
    launch_providers = config.get("launch_providers")

    if isinstance(launch_providers, dict):
        for provider_id, value in launch_providers.items():
            if not isinstance(value, dict):
                continue
            backend = value.get("backend")
            if not backend:
                continue
            specs.append(
                {
                    "id": str(provider_id),
                    "label": value.get("label") or str(provider_id),
                    "backend": str(backend),
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
            specs.append(
                {
                    "id": str(provider_id),
                    "label": value.get("label") or str(provider_id),
                    "backend": str(backend),
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
        launch_fields = spec.get("launch_fields")
        if not isinstance(launch_fields, list):
            launch_fields = _default_launch_fields_for_backend(backend, config)
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
                "defaults": defaults,
                "launch_fields": launch_fields,
                "launch_panels": launch_panels,
            }
        )
    return normalized


def _build_backend_provider(config: dict, backend: str):
    if backend == "slurm":
        return SlurmProvider(config)

    if backend == "local":
        cluster_cfg = config.get("cluster", {})
        local_cfg = cluster_cfg.get("local", {})
        local_cfg = {**cluster_cfg, **local_cfg}
        return LocalProvider(local_cfg)

    raise RuntimeError(f"Unsupported cluster backend: {backend}")


def build_providers(config: dict, provider_specs: list[dict]):
    providers = {}
    for backend in {str(spec.get("backend")) for spec in provider_specs}:
        if not backend:
            continue
        providers[backend] = _build_backend_provider(config, backend)
    return providers
