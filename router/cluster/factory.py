from .slurm import SlurmProvider


def build_provider(config: dict):
    cluster_cfg = config.get("cluster", {})
    backend = cluster_cfg.get("backend", "slurm")

    if backend == "slurm":
        return SlurmProvider(config)

    raise RuntimeError(f"Unsupported cluster backend: {backend}")
