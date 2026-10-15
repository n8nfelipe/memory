from __future__ import annotations

import re
from pathlib import Path


def project_for_directory(directory: Path | None = None) -> str:
    """Use the nearest Git root as project scope, falling back to the cwd name."""
    current = (directory or Path.cwd()).resolve()
    root = next((parent for parent in (current, *current.parents) if (parent / ".git").exists()), current)
    name = root.name or "default"
    normalized = re.sub(r"[^a-zA-Z0-9._-]+", "-", name).strip("-._").lower()
    return (normalized or "default")[:200]
