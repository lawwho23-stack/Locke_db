"""Merge native user hooks without replacing existing settings or project mappings."""

import argparse
import json
import os
import selectors
import shlex
import shutil
import subprocess
import sys
import time
import tomllib
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


def private_write(path: Path, content: str, mode: int = 0o600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if path.is_symlink():
        raise ValueError("Refusing symlink configuration destination.")
    temporary = path.with_name(path.name + ".locke-tmp")
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
    try:
        with os.fdopen(fd, "w") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        temporary.replace(path)
        path.chmod(mode)
    finally:
        temporary.unlink(missing_ok=True)


def install(*, home: Path | None = None) -> dict[str, Any]:
    home = home or Path.home()
    launcher = home / ".local/bin/locke-memory-hook"
    # An absolute venv interpreter avoids uv downloads and shell PATH differences.
    content = "#!/bin/sh\nexec " + shlex.quote(sys.executable) + ' -m memory_platform.hooks "$@"\n'
    changed = []
    if not launcher.exists() or launcher.read_text() != content:
        private_write(launcher, content, 0o700)
        changed.append(str(launcher))
    for name, path in (
        ("claude", home / ".claude/settings.json"),
        ("codex", home / ".codex/hooks.json"),
    ):
        if path.is_symlink():
            raise ValueError("Refusing symlink settings file.")
        original = path.read_text() if path.exists() else "{}"
        data = json.loads(original)
        groups = data.setdefault("hooks", {})
        command = shlex.quote(str(launcher)) + " event --client " + name
        for event in ("SessionStart", "UserPromptSubmit", "Stop"):
            entries = groups.setdefault(event, [])
            entry = {"hooks": [{"type": "command", "command": command, "timeout": 20}]}
            owned = [
                i
                for i, old in enumerate(entries)
                if any(hook.get("command") == command for hook in old.get("hooks", []))
            ]
            if owned:
                # Only replace a group wholly owned by this installer.
                index = owned[0]
                if len(entries[index].get("hooks", [])) != 1:
                    raise ValueError("Locke hook shares a group; merge manually.")
                entries[index] = entry
            else:
                entries.append(entry)
        if data != json.loads(original):
            if path.exists():
                stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%f")
                private_write(path.with_name(path.name + ".locke-backup-" + stamp), original)
            private_write(path, json.dumps(data, indent=2) + "\n")
            changed.append(str(path))
    mapping = home / ".config/locke/hooks.json"
    if not mapping.exists():
        private_write(mapping, '{"projects": []}\n')
        changed.append(str(mapping))
    return {"changed": changed, "mapping": str(mapping), "launcher": str(launcher)}


def configure_codex() -> dict[str, Any]:
    """Use the native config API to trust only our definitions and allow journal writes."""
    if shutil.which("codex") is None:
        return {"configured": False, "reason": "Codex is not installed."}
    home = Path.home()
    config = home / ".codex/config.toml"
    roots = (
        tomllib.loads(config.read_text())
        .get("sandbox_workspace_write", {})
        .get("writable_roots", [])
        if config.exists()
        else []
    )
    journal_root = str(home / ".local/state/locke")
    (home / ".local/state/locke").mkdir(parents=True, exist_ok=True, mode=0o700)
    roots = list(dict.fromkeys([*roots, journal_root]))
    process = subprocess.Popen(
        ["codex", "app-server", "--stdio"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        bufsize=0,
    )
    assert process.stdin is not None and process.stdout is not None
    selector = selectors.DefaultSelector()
    selector.register(process.stdout, selectors.EVENT_READ)

    buffer = b""

    def request(number: int, method: str, params: dict[str, Any]) -> dict[str, Any]:
        nonlocal buffer
        assert process.stdin is not None and process.stdout is not None
        process.stdin.write(
            (json.dumps({"id": number, "method": method, "params": params}) + "\n").encode()
        )
        process.stdin.flush()
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            if b"\n" not in buffer:
                if not selector.select(0.5):
                    continue
                chunk = os.read(process.stdout.fileno(), 65536)
                if not chunk:
                    raise ValueError("Codex config server closed.")
                buffer += chunk
                if b"\n" not in buffer:
                    continue
            line, buffer = buffer.split(b"\n", 1)
            response = json.loads(line)
            if response.get("id") == number:
                if "error" in response:
                    raise ValueError("Codex rejected the hook configuration.")
                return dict(response["result"])
        raise TimeoutError("Codex config request timed out.")

    try:
        request(
            1,
            "initialize",
            {
                "clientInfo": {"name": "locke-hook-installer", "version": "1"},
                "capabilities": {"experimentalApi": True},
            },
        )
        process.stdin.write(b'{"method":"initialized"}\n')
        process.stdin.flush()
        listing = request(2, "hooks/list", {"cwds": [str(Path.cwd())]})
        command = shlex.quote(str(home / ".local/bin/locke-memory-hook")) + " event --client codex"
        ours = [
            hook
            for entry in listing["data"]
            for hook in entry["hooks"]
            if hook.get("command") == command
            and hook["source"] == "user"
            and hook["sourcePath"] == str(home / ".codex/hooks.json")
        ]
        if len(ours) != 3:
            raise ValueError("Expected exactly three installed Locke hooks.")
        edits = [
            {
                "keyPath": "hooks.state." + json.dumps(hook["key"]) + ".trusted_hash",
                "value": hook["currentHash"],
                "mergeStrategy": "upsert",
            }
            for hook in ours
        ]
        edits.append(
            {
                "keyPath": "sandbox_workspace_write.writable_roots",
                "value": roots,
                "mergeStrategy": "replace",
            }
        )
        if config.exists():
            stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%f")
            private_write(
                config.with_name(config.name + ".locke-backup-" + stamp), config.read_text()
            )
        request(3, "config/batchWrite", {"edits": edits, "reloadUserConfig": True})
        checked = request(4, "hooks/list", {"cwds": [str(Path.cwd())]})
        trust = [
            hook["trustStatus"]
            for entry in checked["data"]
            for hook in entry["hooks"]
            if hook.get("command") == command and hook["source"] == "user"
        ]
        return {"configured": trust == ["trusted"] * 3, "journal_writable_root": journal_root}
    finally:
        selector.close()
        process.terminate()
        process.wait(timeout=5)


def main() -> int:
    argparse.ArgumentParser(description=__doc__).parse_args()
    try:
        result = install()
        result["codex"] = configure_codex()
        print(json.dumps(result, indent=2))
    except (OSError, ValueError, TypeError):
        print("Could not merge Locke hooks; existing configuration was preserved.", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
