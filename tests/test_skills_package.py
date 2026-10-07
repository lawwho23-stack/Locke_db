"""Approved packages preserve bytes and reject unsafe selected folders."""

import base64
import importlib

import pytest


def module():
    return importlib.import_module("memory_platform.skill_package")


def file(path, data=b"hello\r\n", executable=False):
    return {
        "path": path,
        "content_base64": base64.b64encode(data).decode(),
        "executable": executable,
    }


def test_package_preserves_exact_bytes_and_hashes_modes():
    m = module()
    files = [file("SKILL.md", "မြန်မာ\r\n".encode()), file("scripts/a.py", b"\0\xff", True)]
    validated = m.validate_package(files)
    assert validated[0].data == "မြန်မာ\r\n".encode()
    assert validated[1].data == b"\0\xff"
    assert m.package_hash(files) == m.package_hash(list(reversed(files)))
    assert m.package_hash(files) != m.package_hash([file("SKILL.md", "မြန်မာ\n".encode()), files[1]])
    assert m.package_hash(files) != m.package_hash([files[0], file("scripts/a.py", b"\0\xff")])


@pytest.mark.parametrize(
    "path", ["../x", "/x", "a/../x", "a\\x", ".env", ".git/config", "a//x", "a/./x", "secrets.pem"]
)
def test_rejects_unsafe_paths(path):
    with pytest.raises(ValueError):
        module().validate_package([file("SKILL.md"), file(path)])


def test_rejects_collisions_missing_entrypoint_and_limits():
    m = module()
    for files in (
        [file("SKILL.md"), file("skill.md")],
        [file("a.txt")],
        [file("SKILL.md", b"x" * (1024 * 1024 + 1))],
    ):
        with pytest.raises(ValueError):
            m.validate_package(files)


def test_pack_directory_rejects_symlinks(tmp_path):
    (tmp_path / "SKILL.md").write_bytes(b"hello\r\n")
    (tmp_path / "link").symlink_to(tmp_path / "SKILL.md")
    with pytest.raises(ValueError):
        module().pack_directory(tmp_path)


def test_unicode_directory_file_collision_is_rejected():
    with pytest.raises(ValueError):
        module().validate_package([file("SKILL.md"), file("é"), file("e\u0301/x")])


def test_package_total_and_count_limits():
    with pytest.raises(ValueError):
        module().validate_package(
            [file("SKILL.md")] + [file(f"assets/{n}", b"x" * 1048576) for n in range(7)]
        )
    with pytest.raises(ValueError):
        module().validate_package([file("SKILL.md")] + [file(f"assets/{n}") for n in range(128)])


def test_invalid_base64_rejected():
    with pytest.raises(ValueError):
        module().validate_package([{"path": "SKILL.md", "content_base64": "not-base64"}])
