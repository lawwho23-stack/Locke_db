"""Owner skill import and explicit agent operations over the shared HTTP API."""

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

import httpx

from memory_platform.client import APIClient, ClientError
from memory_platform.outbox import Outbox


def _print(data: Any) -> None:
    print(json.dumps(data, ensure_ascii=False, indent=2, default=str))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="memory")
    parser.add_argument("--url", default=os.environ.get("MEMORY_API_URL", "http://127.0.0.1:8000"))
    commands = parser.add_subparsers(dest="command", required=True)
    preview = commands.add_parser("skill-preview", help="Preview locally; nothing is uploaded")
    preview.add_argument("folder", type=Path)
    store = commands.add_parser("skill-store", help="Owner-approved exact package import")
    store.add_argument("folder", type=Path)
    store.add_argument("--scope", required=True)
    store.add_argument("--keyword", required=True)
    store.add_argument("--approved-hash", required=True)
    store.add_argument("--expected-revision", type=int, default=0)
    resolve = commands.add_parser("skill-resolve")
    resolve.add_argument("keyword")
    resolve.add_argument("--scope", required=True)
    install = commands.add_parser(
        "skill-install", help="Install approved package; never execute it"
    )
    install.add_argument("keyword")
    install.add_argument("--scope", required=True)
    install.add_argument("--destination", type=Path, required=True)
    tasks = commands.add_parser("tasks")
    tasks.add_argument("--scope", required=True)
    request = commands.add_parser("request", help="Explicit API operation; JSON body on stdin")
    request.add_argument("method", choices=["GET", "POST", "PATCH", "DELETE"])
    request.add_argument("path")
    request.add_argument("--queue", action="store_true", help="Queue a task event before sending")
    request.add_argument("--idempotency-key")
    for name in ("sync", "outbox"):
        command = commands.add_parser(name)
        command.add_argument(
            "--queue-path",
            type=Path,
            default=Path.home() / ".local/state/memory-platform/outbox.sqlite",
        )
    for name, help_text in (
        ("outbox-retry", "Requeue a blocked event with its original payload"),
        ("outbox-discard", "Discard a blocked event without changing its payload"),
    ):
        command = commands.add_parser(name, help=help_text)
        command.add_argument("event_id")
        command.add_argument(
            "--queue-path",
            type=Path,
            default=Path.home() / ".local/state/memory-platform/outbox.sqlite",
        )
    args = parser.parse_args(argv)
    try:
        if args.command in {"skill-preview", "skill-store"}:
            from memory_platform.skill_package import pack_directory, package_hash

            files = pack_directory(args.folder)
            digest = package_hash(files)
            if args.command == "skill-preview":
                _print(
                    {
                        "package_hash": digest,
                        "files": [{"path": f.path, "executable": f.executable} for f in files],
                        "approval": "Approve this exact hash before storing.",
                    }
                )
                return 0
            if args.approved_hash != digest:
                raise ValueError("Approved hash does not match the current package.")
        if args.command == "outbox":
            with Outbox(args.queue_path) as queue:
                _print(queue.status())
            return 0
        if args.command in ("outbox-retry", "outbox-discard"):
            with Outbox(args.queue_path) as queue:
                if args.command == "outbox-retry":
                    queue.retry(args.event_id)
                else:
                    queue.discard(args.event_id)
                _print(queue.status())
            return 0
        token = os.environ.get("MEMORY_API_TOKEN")
        if not token:
            raise ValueError("Set MEMORY_API_TOKEN to your scoped credential.")
        with APIClient(args.url, token) as client:
            if args.command == "skill-store":
                _print(
                    client.request(
                        "POST",
                        "/v1/skills",
                        {
                            "scope_id": args.scope,
                            "command": args.keyword,
                            "files": [f.model_dump() for f in files],
                            "approved_package_hash": digest,
                            "expected_revision": args.expected_revision,
                        },
                    )
                )
            elif args.command in {"skill-resolve", "skill-install"}:
                package = client.resolve_skill(args.scope, args.keyword)
                if args.command == "skill-install":
                    from memory_platform.install import install_package

                    install_package(package, args.destination)
                    _print(
                        {
                            "installed": str(args.destination),
                            "package_hash": package["package_hash"],
                        }
                    )
                else:
                    _print(package)
            elif args.command == "tasks":
                _print(client.tasks(args.scope))
            elif args.command == "sync":
                with Outbox(args.queue_path) as queue:
                    _print(queue.flush(client))
            else:
                payload = json.loads(sys.stdin.read(65537)) if args.method != "GET" else None
                if payload is not None and not isinstance(payload, dict):
                    raise ValueError("Body must be a JSON object.")
                if args.path == "/v1/skills" and args.method == "POST":
                    raise ValueError("Use skill-store with an explicitly approved package hash.")
                if args.queue:
                    with Outbox(
                        Path.home() / ".local/state/memory-platform/outbox.sqlite"
                    ) as queue:
                        queue.enqueue(args.method, args.path, payload or {})
                        _print(queue.flush(client))
                else:
                    _print(
                        client.request(
                            args.method, args.path, payload, idempotency_key=args.idempotency_key
                        )
                    )
        return 0
    except (ValueError, ClientError, httpx.TransportError, OSError):
        print(
            "Memory command failed. Check inputs, scoped access, connection and outbox status.",
            file=sys.stderr,
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
