"""Atomic write of the impact-queue pair, with output-path containment."""
from __future__ import annotations

import json
import os
import uuid
from pathlib import Path

from .errors import MonitorError

def _remove_quietly(path: Path) -> None:
    try:
        path.unlink()
    except OSError:
        # Cleanup is best effort; the caller re-raises the original failure.
        pass


def _restore_quietly(parked: Path, destination: Path) -> None:
    try:
        os.replace(parked, destination)
    except OSError:
        pass


def _sibling_partial(destination: Path) -> Path:
    """Create a unique staging file beside a queue destination."""
    while True:
        candidate = destination.with_name(
            f"{destination.name}.{uuid.uuid4().hex[:12]}.partial"
        )
        try:
            with candidate.open("x", encoding="utf-8"):
                pass
        except FileExistsError:  # pragma: no cover - a 48-bit name collision.
            continue
        return candidate


def _swap_into_place(staged_path: Path, destination: Path) -> Path | None:
    """Commit one staged file while retaining the previous file for rollback."""
    parked: Path | None = None
    if destination.is_file():
        parked = _sibling_partial(destination)
        try:
            os.replace(destination, parked)
        except OSError:
            _remove_quietly(parked)
            raise
    try:
        os.replace(staged_path, destination)
    except OSError:
        if parked is not None:
            _restore_quietly(parked, destination)
        raise
    return parked


def write_queue_files(json_text: str, markdown_text: str, output_dir: Path) -> dict[str, Path]:
    """Stage and commit the JSON/Markdown pair, restoring the old pair on failure."""
    if not output_dir.is_absolute():
        root = Path.cwd().resolve()
        resolved = (root / output_dir).resolve()
        try:
            resolved.relative_to(root)
        except ValueError as exc:
            raise MonitorError(f"output directory must stay within {root}.") from exc
        output_dir = resolved
    output_dir.mkdir(parents=True, exist_ok=True)
    json_path = output_dir / "impact-queue.json"
    markdown_path = output_dir / "impact-queue.md"
    rendered = (
        (json_path, json_text),
        (markdown_path, markdown_text),
    )

    staged: list[tuple[Path, Path]] = []
    try:
        for destination, text in rendered:
            staged_path = _sibling_partial(destination)
            staged.append((staged_path, destination))
            staged_path.write_text(text, encoding="utf-8")
    except BaseException:
        for staged_path, _ in staged:
            _remove_quietly(staged_path)
        raise

    replaced: list[tuple[Path, Path | None]] = []
    try:
        for staged_path, destination in staged:
            replaced.append(
                (destination, _swap_into_place(staged_path, destination))
            )
    except OSError:
        for destination, parked in reversed(replaced):
            if parked is None:
                _remove_quietly(destination)
            else:
                _restore_quietly(parked, destination)
        for staged_path, _ in staged:
            _remove_quietly(staged_path)
        raise
    for _, parked in replaced:
        if parked is not None:
            _remove_quietly(parked)
    return {"json": json_path, "markdown": markdown_path}



