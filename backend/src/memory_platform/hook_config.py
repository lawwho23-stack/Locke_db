"""Explicit project allowlist; credentials remain in environment or macOS Keychain."""

import getpass
import json
import os
import subprocess
from pathlib import Path
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

CONFIG_PATH = Path.home() / ".config/locke/hooks.json"
STATE_PATH = Path.home() / ".local/state/locke/hooks.sqlite"


class CredentialRef(BaseModel):
    model_config = ConfigDict(extra="forbid")
    token_env: str | None = Field(default=None, pattern=r"^[A-Z][A-Z0-9_]*$")
    keychain_service: str | None = Field(default=None, min_length=1, max_length=200)

    @model_validator(mode="after")
    def one_source(self) -> "CredentialRef":
        if bool(self.token_env) == bool(self.keychain_service):
            raise ValueError("Choose one credential reference.")
        return self

    def token(self) -> str:
        if self.token_env:
            value = os.environ.get(self.token_env, "")
        else:
            result = subprocess.run(
                [
                    "/usr/bin/security",
                    "find-generic-password",
                    "-a",
                    getpass.getuser(),
                    "-s",
                    self.keychain_service or "",
                    "-w",
                ],
                capture_output=True,
                text=True,
                timeout=3,
            )
            value = result.stdout.strip() if result.returncode == 0 else ""
        if not value:
            raise ValueError("Scoped Locke credential unavailable.")
        return value


class Project(BaseModel):
    model_config = ConfigDict(extra="forbid")
    root: Path
    scope_id: UUID
    api_url: str = "https://locke-db-api.vercel.app"
    query: str = Field(min_length=1, max_length=1000)
    clients: dict[str, CredentialRef]

    @field_validator("clients")
    @classmethod
    def supported_clients(cls, value: dict[str, CredentialRef]) -> dict[str, CredentialRef]:
        if not value or not value.keys() <= {"claude", "codex"}:
            raise ValueError("Choose Claude and/or Codex credential references.")
        return value


class HookConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    projects: list[Project] = Field(default_factory=list, max_length=200)


def project_for(cwd: Path, client_name: str, path: Path = CONFIG_PATH) -> Project | None:
    if not path.exists():
        return None
    if path.is_symlink() or path.stat().st_mode & 0o077:
        raise ValueError("Hook mapping must be a private regular file (chmod 600).")
    config = HookConfig.model_validate(json.loads(path.read_text()))
    here = cwd.resolve()
    matches = []
    roots = set()
    for project in config.projects:
        if not project.root.is_absolute():
            raise ValueError("Project roots must be absolute.")
        root = project.root.resolve()
        if root in roots:
            raise ValueError("Duplicate project mapping.")
        roots.add(root)
        if here.is_relative_to(root) and client_name in project.clients:
            matches.append(project)
    return max(matches, key=lambda item: len(item.root.resolve().parts), default=None)
