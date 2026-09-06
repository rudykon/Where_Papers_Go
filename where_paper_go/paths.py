"""Central project paths shared by CLI, web, and retrieval workers."""

import os
from pathlib import Path


PACKAGE_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = PACKAGE_DIR.parent


def _configured_path(name: str, default: Path) -> Path:
    raw_value = os.environ.get(name, "").strip()
    if not raw_value:
        return default
    path = Path(raw_value).expanduser()
    if not path.is_absolute():
        raise RuntimeError(f"{name} must be an absolute path")
    return path.resolve()


DATA_DIR = _configured_path("WPG_DATA_DIR", PROJECT_ROOT / "data")
WEB_DIR = PACKAGE_DIR / "static"
DEFAULT_CONFIG_PATH = _configured_path(
    "WPG_API_CONFIG", PROJECT_ROOT / "llmapi.json"
)
