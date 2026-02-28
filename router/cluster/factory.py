from .slurm import SlurmProvider
from ..providers.local_provider import LocalProvider


def build_provider(config: dict):
    cluster_cfg = config.get("cluster", {})
    backend = cluster_cfg.get("backend", "slurm")

    if backend == "slurm":
        return SlurmProvider(config)

    if backend == "local":
        local_cfg = cluster_cfg.get("local", {})
        return LocalProvider(local_cfg)

    raise RuntimeError(f"Unsupported cluster backend: {backend}")
