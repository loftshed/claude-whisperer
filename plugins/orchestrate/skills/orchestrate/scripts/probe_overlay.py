#!/usr/bin/env python3
"""Run a candidate test command in a disposable, out-of-tree source overlay."""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

CONFIG_FILES = (
    ".editorconfig",
    ".gitignore",
    ".markdownlint-cli2.jsonc",
    ".prettierignore",
    ".prettierrc.json",
    ".ruff.toml",
    ".shellcheckrc",
    ".yarnrc.yml",
    "cspell.json",
    "eslint.config.js",
    "package.json",
    "release.json",
    "ruff.toml",
    "vitest.config.js",
    "yarn.lock",
)


def _repo_root(path: Path) -> Path:
    root = path.expanduser().resolve(strict=True)
    if not root.is_dir():
        raise ValueError(f"repository root is not a directory: {root}")
    return root


def _copy_file(source_root: Path, overlay: Path, relative: str) -> None:
    relative_path = Path(relative)
    if relative_path.is_absolute() or not relative or ".." in relative_path.parts:
        raise ValueError(f"copy path must be relative and stay inside the repository: {relative!r}")
    source = source_root / relative_path
    if source.is_symlink():
        raise ValueError(f"copy path cannot be a symlink: {relative!r}")
    resolved = source.resolve(strict=True)
    if not resolved.is_relative_to(source_root) or not resolved.is_file():
        raise ValueError(f"copy path must name a file inside the repository: {relative!r}")
    destination = overlay / relative_path
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(resolved, destination)


def _apply_patch(overlay: Path, patch_path: Path) -> None:
    patch = patch_path.expanduser().resolve(strict=True)
    if not patch.is_file():
        raise ValueError(f"patch path is not a file: {patch}")
    checked = subprocess.run(["git", "apply", "--check", str(patch)], cwd=overlay, capture_output=True, text=True)
    if checked.returncode:
        detail = checked.stderr.strip() or checked.stdout.strip()
        raise ValueError(f"candidate patch could not be applied: {detail}")
    applied = subprocess.run(["git", "apply", str(patch)], cwd=overlay, capture_output=True, text=True)
    if applied.returncode:
        detail = applied.stderr.strip() or applied.stdout.strip()
        raise ValueError(f"candidate patch could not be applied: {detail}")
    for current, directories, files in os.walk(overlay, followlinks=False):
        for name in (*directories, *files):
            if (Path(current) / name).is_symlink():
                raise ValueError("candidate patch may not add symlinks to the probe overlay")


def _link_shared_inputs(source_root: Path, overlay: Path) -> None:
    dependencies = source_root / "node_modules"
    if dependencies.exists():
        destination = overlay / "node_modules"
        if not destination.exists() and not destination.is_symlink():
            destination.symlink_to(dependencies, target_is_directory=True)

    for name in CONFIG_FILES:
        source = source_root / name
        destination = overlay / name
        if source.is_file() and not source.is_symlink() and not destination.exists() and not destination.is_symlink():
            destination.symlink_to(source)


@contextmanager
def probe_overlay(
    repo_root: Path | str,
    *,
    copy_paths: tuple[str, ...] | list[str] = (),
    patch_path: Path | str | None = None,
    tmp_dir: Path | str | None = None,
) -> Iterator[Path]:
    """Yield a disposable overlay with requested files and optional patch applied."""
    source_root = _repo_root(Path(repo_root))
    if not copy_paths and patch_path is None:
        raise ValueError("provide at least one --copy path or --patch file")
    temporary_root = Path(tmp_dir or "/tmp").expanduser().resolve()
    if not temporary_root.is_dir():
        raise ValueError(f"temporary directory does not exist: {temporary_root}")

    with tempfile.TemporaryDirectory(prefix="ork-probe-", dir=temporary_root) as temporary:
        overlay = Path(temporary)
        for relative in copy_paths:
            _copy_file(source_root, overlay, relative)
        if patch_path is not None:
            _apply_patch(overlay, Path(patch_path))
        _link_shared_inputs(source_root, overlay)
        yield overlay


def run_probe(repo_root: Path, copy_paths: list[str], patch_path: Path | None, command: list[str]) -> int:
    if not command:
        raise ValueError("provide a test command after --")
    with probe_overlay(repo_root, copy_paths=copy_paths, patch_path=patch_path) as overlay:
        environment = os.environ.copy()
        environment["ORK_PROBE_OVERLAY"] = str(overlay)
        environment["ORK_PROBE_SOURCE_ROOT"] = str(_repo_root(repo_root))
        scratch = overlay / ".probe-tmp"
        cache = overlay / ".probe-cache"
        scratch.mkdir()
        cache.mkdir()
        environment.update(
            {
                "TMPDIR": str(scratch),
                "TMP": str(scratch),
                "TEMP": str(scratch),
                "XDG_CACHE_HOME": str(cache),
                "npm_config_cache": str(cache / "npm"),
                "YARN_CACHE_FOLDER": str(cache / "yarn"),
                "PIP_CACHE_DIR": str(cache / "pip"),
                "UV_CACHE_DIR": str(cache / "uv"),
            }
        )
        try:
            completed = subprocess.run(command, cwd=overlay, env=environment, check=False)
        except OSError as error:
            print(f"probe-overlay: could not start command: {error}", file=sys.stderr)
            return 127
        return completed.returncode


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, default=Path.cwd(), help="source repository root (default: cwd)")
    parser.add_argument("--copy", action="append", default=[], metavar="PATH", help="copy a repository-relative file")
    parser.add_argument("--patch", type=Path, help="apply a candidate git-format patch from any path")
    parser.add_argument("command", nargs=argparse.REMAINDER, help="test command after --; executed without a shell")
    args = parser.parse_args(argv)
    command = args.command[1:] if args.command[:1] == ["--"] else args.command
    try:
        return run_probe(args.repo, args.copy, args.patch, command)
    except (OSError, ValueError) as error:
        print(f"probe-overlay: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
