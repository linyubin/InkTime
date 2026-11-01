"""Safe containment checks for server and WebUI file routes."""
from pathlib import Path


def safe_join(base: Path, rel: str) -> Path:
    """Resolve a relative path strictly inside *base*, including symlink checks."""
    relative = Path(rel)
    if relative.is_absolute():
        raise ValueError("absolute paths are not allowed")
    root = Path(base).resolve()
    candidate = (root / relative).resolve()
    try:
        candidate.relative_to(root)
    except ValueError as exc:
        raise ValueError("path traversal blocked") from exc
    return candidate
