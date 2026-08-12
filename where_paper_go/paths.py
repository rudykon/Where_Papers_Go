"""Central project paths shared by CLI, web, and retrieval workers."""

from pathlib import Path


PACKAGE_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = PACKAGE_DIR.parent
DATA_DIR = PROJECT_ROOT / "data"
WEB_DIR = PACKAGE_DIR / "static"
DEFAULT_CONFIG_PATH = PROJECT_ROOT / "llmapi.json"
