#!/usr/bin/env python3
"""Install generated sync artifacts without deleting user-authored config."""

from __future__ import annotations

import argparse
import shutil
from pathlib import Path


class ArtifactInstallError(RuntimeError):
    pass


ROOT_ARTIFACTS = (
    "fingerprint.json",
    "static_analysis.pkl",
    "static_analysis.sha",
    "codeboarding_version.json",
)


def _remove_file(path: Path) -> bool:
    if not path.exists() and not path.is_symlink():
        return False
    if path.is_dir():
        raise ArtifactInstallError(f"Generated artifact path is a directory: {path}")
    path.unlink()
    return True


def _replace_optional(source: Path, target: Path, stage_paths: set[Path]) -> None:
    existed = _remove_file(target)
    if source.is_file():
        shutil.copy2(source, target)
        stage_paths.add(target)
    elif existed:
        stage_paths.add(target)


def install_sync_artifacts(
    *,
    output_dir: Path,
    docs_dir: Path,
    analysis_path: Path,
    analysis_dir: Path,
) -> list[Path]:
    """Replace only action-owned artifacts and return paths that should be staged."""

    rendered_docs = sorted(docs_dir.glob("*.md"))
    if not rendered_docs:
        raise ArtifactInstallError(f"No rendered markdown files found in: {docs_dir}")
    if not analysis_path.is_file():
        raise ArtifactInstallError(f"Missing analysis artifact: {analysis_path}")

    stage_paths: set[Path] = set()
    if output_dir.is_dir():
        for old_doc in output_dir.glob("*.md"):
            _remove_file(old_doc)
            stage_paths.add(old_doc)

    output_dir.mkdir(parents=True, exist_ok=True)
    health_dir = output_dir / "health"
    health_dir.mkdir(parents=True, exist_ok=True)

    for source in rendered_docs:
        target = output_dir / source.name
        _remove_file(target)
        shutil.copy2(source, target)
        stage_paths.add(target)

    analysis_target = output_dir / "analysis.json"
    _remove_file(analysis_target)
    shutil.copy2(analysis_path, analysis_target)
    stage_paths.add(analysis_target)

    for name in ROOT_ARTIFACTS:
        _replace_optional(analysis_dir / name, output_dir / name, stage_paths)

    _replace_optional(
        analysis_dir / "health" / "health_report.json",
        health_dir / "health_report.json",
        stage_paths,
    )

    return sorted(stage_paths)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--docs-dir", required=True, type=Path)
    parser.add_argument("--analysis", required=True, type=Path)
    parser.add_argument("--analysis-dir", required=True, type=Path)
    args = parser.parse_args(argv)

    try:
        paths = install_sync_artifacts(
            output_dir=args.output_dir,
            docs_dir=args.docs_dir,
            analysis_path=args.analysis,
            analysis_dir=args.analysis_dir,
        )
    except ArtifactInstallError as exc:
        parser.error(str(exc))

    for path in paths:
        print(path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
