from __future__ import annotations

import os
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class _DirectoryScanCache:
    canonical_paths: dict[str, str] = field(default_factory=dict)
    comparisons: dict[tuple[str, str], bool] = field(default_factory=dict)


_active_directory_scan_cache: ContextVar[_DirectoryScanCache | None] = ContextVar(
    "directory_scan_cache", default=None
)


@contextmanager
def directory_scan_cache() -> Iterator[None]:
    """Reuse path checks within one scan, then discard that scan's results."""

    token = _active_directory_scan_cache.set(_DirectoryScanCache())
    try:
        yield
    finally:
        _active_directory_scan_cache.reset(token)


def canonical_path(path: str | os.PathLike[str]) -> str:
    """Return a native-platform path key without Windows/WSL translation."""

    cache = _active_directory_scan_cache.get()
    key = os.fspath(path)
    if cache is not None and key in cache.canonical_paths:
        return cache.canonical_paths[key]

    resolved = Path(path).expanduser().resolve(strict=False)
    normalized = os.path.normpath(str(resolved))
    if os.name == "nt":
        normalized = os.path.normcase(normalized)
    if cache is not None:
        cache.canonical_paths[key] = normalized
    return normalized


def same_directory(
    left: str | os.PathLike[str], right: str | os.PathLike[str]
) -> bool:
    """Return whether two path spellings identify the same directory."""

    cache = _active_directory_scan_cache.get()
    key = (os.fspath(left), os.fspath(right))
    if cache is not None and key in cache.comparisons:
        return cache.comparisons[key]

    left_path = canonical_path(left)
    right_path = canonical_path(right)
    if left_path == right_path:
        result = True
    else:
        try:
            result = os.path.samefile(left_path, right_path)
        except OSError:
            result = False
    if cache is not None:
        cache.comparisons[key] = result
    return result


def is_within(path: Path, root: Path) -> bool:
    try:
        path.resolve(strict=False).relative_to(root.resolve(strict=False))
    except ValueError:
        return False
    return True
