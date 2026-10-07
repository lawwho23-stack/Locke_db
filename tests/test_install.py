import base64
from pathlib import Path

import pytest

from memory_platform.api.skill_schemas import SkillFile
from memory_platform.install import install_package
from memory_platform.skill_package import package_hash


def package():
    files = [
        SkillFile(path="SKILL.md", content_base64=base64.b64encode(b"approved\r\n").decode()),
        SkillFile(
            path="scripts/run.sh",
            content_base64=base64.b64encode(b"exit 7\n").decode(),
            executable=True,
        ),
    ]
    return {"files": [f.model_dump() for f in files], "package_hash": package_hash(files)}


def test_safe_install_never_executes_and_refuses_overwrite(tmp_path: Path):
    dest = tmp_path / "github-review"
    install_package(package(), dest)
    assert (dest / "SKILL.md").read_bytes() == b"approved\r\n"
    assert (dest / "scripts/run.sh").stat().st_mode & 0o777 == 0o755
    with pytest.raises(ValueError):
        install_package(package(), dest)


def test_tampered_package_or_symlink_parent_rejected(tmp_path: Path):
    data = package()
    data["package_hash"] = "0" * 64
    with pytest.raises(ValueError):
        install_package(data, tmp_path / "rejected")
    assert not (tmp_path / "rejected").exists()
    (tmp_path / "link").symlink_to(tmp_path, target_is_directory=True)
    with pytest.raises(ValueError):
        install_package(package(), tmp_path / "link" / "unsafe")
