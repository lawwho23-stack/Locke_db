"""Vercel FastAPI entry point; local Uvicorn factory remains available."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))

from memory_platform.api.app import create_default_app

app = create_default_app()
