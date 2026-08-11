from __future__ import annotations

import os
from pathlib import Path


def canonical_path(path: str | os.PathLike[str]) -> str:
    """Return a native-platform path key without Windows/WSL translation."""

    resolved = Path(path).expanduser().resolve(strict=False)
    normalized = os.path.normpath(str(resolved))
    if os.name == "nt":
        normalized = os.path.normcase(normalized)
    return normalized


def is_within(path: Path, root: Path) -> bool:
    try:
        path.resolve(strict=False).relative_to(root.resolve(strict=False))
    except ValueError:
        return False
    return True

