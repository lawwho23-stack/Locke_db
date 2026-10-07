"""Verify and install a complete approved package into a new managed directory."""

import os
import shutil
import tempfile
from pathlib import Path
from typing import Any

from memory_platform.api.skill_schemas import SkillFile
from memory_platform.skill_package import package_hash, validate_package


def install_package(package: dict[str, Any], destination: Path) -> None:
    files = [SkillFile.model_validate(item) for item in package["files"]]
    validate_package(files)
    if package_hash(files) != package.get("package_hash"):
        raise ValueError("Package integrity check failed.")
    destination = destination.absolute()
    if any(parent.is_symlink() for parent in (destination, *destination.parents)):
        raise ValueError("Installation paths cannot contain symlinks.")
    if destination.exists():
        raise ValueError("Destination already exists; choose a new version directory.")
    destination.parent.mkdir(parents=True, exist_ok=True)
    stage = Path(tempfile.mkdtemp(prefix=".memory-skill-", dir=destination.parent))
    try:
        import base64

        for item in files:
            target = stage / item.path
            target.parent.mkdir(mode=0o755, parents=True, exist_ok=True)
            target.write_bytes(base64.b64decode(item.content_base64, validate=True))
            target.chmod(0o755 if item.executable else 0o644)
        # A managed install never overlays another skill's files.
        if destination.exists() or destination.is_symlink():
            raise ValueError("Installation destination appeared during staging.")
        os.rename(stage, destination)
    finally:
        if stage.exists():
            shutil.rmtree(stage)
