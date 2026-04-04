#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import re
import shutil
import tarfile
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]

REQUIRED_PATHS = {
    "python wheel": lambda root: next((root / "dist").glob("codeswarm-*.whl"), None),
    "CLI build": lambda root: root / "cli" / "dist" / "index.js",
    "backend build": lambda root: root / "web" / "backend" / "dist" / "server.js",
    "frontend standalone": lambda root: next(
        (root / "web" / "frontend" / ".next" / "standalone").rglob("server.js"),
        None,
    ),
    "frontend static": lambda root: root / "web" / "frontend" / ".next" / "static",
}

COPY_PATHS = [
    "README.md",
    "LICENSE",
    "install-codeswarm.sh",
    "pyproject.toml",
    "bootstrap.sh",
    "agent",
    "common",
    "router",
    "slurm",
    "ssh",
    "configs",
    "cli/dist",
    "cli/package.json",
    "cli/package-lock.json",
    "web/backend/dist",
    "web/backend/package.json",
    "web/backend/package-lock.json",
    "web/frontend/public",
    "web/frontend/.next/standalone",
    "web/frontend/.next/static",
]


def read_version() -> str:
    text = (REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    match = re.search(r'^version\s*=\s*"([^"]+)"', text, re.MULTILINE)
    if not match:
        raise RuntimeError("Unable to determine version from pyproject.toml")
    return match.group(1)


def ensure_required_artifacts() -> Path:
    wheel_path: Path | None = None
    for label, resolver in REQUIRED_PATHS.items():
        resolved = resolver(REPO_ROOT)
        if resolved is None or not Path(resolved).exists():
            raise RuntimeError(
                f"Missing required artifact: {label}. Build release prerequisites before bundling."
            )
        if label == "python wheel":
            wheel_path = Path(resolved)
    assert wheel_path is not None
    return wheel_path


def copy_path(src_rel: str, bundle_root: Path) -> None:
    src = REPO_ROOT / src_rel
    dest = bundle_root / src_rel
    dest.parent.mkdir(parents=True, exist_ok=True)
    if src.is_dir():
        shutil.copytree(src, dest, dirs_exist_ok=True)
    else:
        shutil.copy2(src, dest)


def create_manifest(bundle_root: Path, version: str, wheel_name: str) -> None:
    manifest = {
        "version": version,
        "python_wheel": f"python/dist/{wheel_name}",
        "cli_entry": "cli/dist/index.js",
        "backend_entry": "web/backend/dist/server.js",
        "frontend_entry": "web/frontend/.next/standalone/server.js",
        "default_local_container_image": "ghcr.io/kalowery/codeswarm-local-worker:latest",
    }
    (bundle_root / "release-manifest.json").write_text(
        json.dumps(manifest, indent=2) + "\n",
        encoding="utf-8",
    )


def populate_frontend_standalone_assets(bundle_root: Path) -> None:
    frontend_root = bundle_root / "web" / "frontend"
    standalone_root = frontend_root / ".next" / "standalone"
    static_src = frontend_root / ".next" / "static"
    public_src = frontend_root / "public"

    if static_src.exists():
        shutil.copytree(
            static_src,
            standalone_root / ".next" / "static",
            dirs_exist_ok=True,
        )
    if public_src.exists():
        shutil.copytree(
            public_src,
            standalone_root / "public",
            dirs_exist_ok=True,
        )


def assemble_bundle(output_dir: Path) -> tuple[Path, Path, Path]:
    version = read_version()
    wheel_path = ensure_required_artifacts()
    bundle_root = output_dir / f"codeswarm-{version}"
    if bundle_root.exists():
        shutil.rmtree(bundle_root)
    bundle_root.mkdir(parents=True)

    for rel_path in COPY_PATHS:
        copy_path(rel_path, bundle_root)

    python_dist = bundle_root / "python" / "dist"
    python_dist.mkdir(parents=True, exist_ok=True)
    shutil.copy2(wheel_path, python_dist / wheel_path.name)

    populate_frontend_standalone_assets(bundle_root)
    create_manifest(bundle_root, version, wheel_path.name)

    versioned_archive = output_dir / f"codeswarm-{version}-full.tar.gz"
    latest_archive = output_dir / "codeswarm-full.tar.gz"
    for archive_path in (versioned_archive, latest_archive):
        if archive_path.exists():
            archive_path.unlink()
        with tarfile.open(archive_path, "w:gz") as tar:
            tar.add(bundle_root, arcname=bundle_root.name)

    return bundle_root, versioned_archive, latest_archive


def main() -> None:
    parser = argparse.ArgumentParser(description="Assemble a Codeswarm release bundle")
    parser.add_argument(
        "--output-dir",
        default=str(REPO_ROOT / "release"),
        help="Directory where the unpacked bundle and tarballs should be written",
    )
    args = parser.parse_args()

    output_dir = Path(args.output_dir).resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    bundle_root, versioned_archive, latest_archive = assemble_bundle(output_dir)
    print(bundle_root)
    print(versioned_archive)
    print(latest_archive)


if __name__ == "__main__":
    main()
