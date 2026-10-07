"""Local owner-admin commands; token output occurs only after a committed bootstrap."""

import argparse
import os
import sys
from collections.abc import Sequence
from pathlib import Path
from uuid import uuid4

from sqlalchemy import Connection, Engine, func, or_, select, update

from memory_platform.api.schemas import ScopeCreateRequest
from memory_platform.auth.principal import Principal
from memory_platform.config import get_settings
from memory_platform.db import make_engine
from memory_platform.enums import ActorKind, Capability, ScopeKind
from memory_platform.errors import AppError, ErrorCode
from memory_platform.services.credentials import bootstrap_workspace
from memory_platform.services.scopes import create_scope
from memory_platform.tables import actors, credential_grants, credentials, scopes


def backfill_owner_permissions(conn: Connection) -> int:
    """Explicit rollout step: expand existing owner-admin grants, never agent grants."""
    rows = (
        conn.execute(
            select(credential_grants)
            .join(credentials, credentials.c.id == credential_grants.c.credential_id)
            .join(actors, actors.c.id == credentials.c.actor_id)
            .where(
                actors.c.kind == ActorKind.owner.value,
                credentials.c.is_admin.is_(True),
                credentials.c.revoked_at.is_(None),
                or_(credentials.c.expires_at.is_(None), credentials.c.expires_at > func.now()),
            )
            .with_for_update(of=credential_grants)
        )
        .mappings()
        .all()
    )
    changed = 0
    for row in rows:
        capabilities = sorted({*row["capabilities"], *(cap.value for cap in Capability)})
        if set(capabilities) == set(row["capabilities"]):
            continue
        conn.execute(
            update(credential_grants)
            .where(
                credential_grants.c.credential_id == row["credential_id"],
                credential_grants.c.scope_id == row["scope_id"],
            )
            .values(capabilities=capabilities)
        )
        conn.execute(
            update(scopes)
            .where(scopes.c.id == row["scope_id"])
            .values(
                grant_revision=scopes.c.grant_revision + 1,
            )
        )
        changed += 1
    return changed


def _owner_principal(conn: Connection) -> Principal:
    """The admin CLI uses database access; it does not persist or request an owner token."""
    row = (
        conn.execute(
            select(
                credentials.c.id,
                credentials.c.actor_id,
                credentials.c.workspace_id,
            )
            .join(actors, actors.c.id == credentials.c.actor_id)
            .where(
                actors.c.kind == ActorKind.owner.value,
                credentials.c.is_admin.is_(True),
                credentials.c.revoked_at.is_(None),
                or_(credentials.c.expires_at.is_(None), credentials.c.expires_at > func.now()),
            )
            .order_by(credentials.c.created_at, credentials.c.id)
            .limit(1)
        )
        .mappings()
        .one_or_none()
    )
    if row is None:
        raise AppError(ErrorCode.bad_request, "Bootstrap an owner workspace first.")
    grants = conn.execute(
        select(
            credential_grants.c.scope_id,
            credential_grants.c.capabilities,
        ).where(credential_grants.c.credential_id == row["id"])
    ).mappings()
    return Principal(
        actor_id=row["actor_id"],
        actor_kind=ActorKind.owner,
        workspace_id=row["workspace_id"],
        credential_id=row["id"],
        is_admin=True,
        grants={
            grant["scope_id"]: frozenset(Capability(cap) for cap in grant["capabilities"])
            for grant in grants
        },
    )


def main(argv: Sequence[str] | None = None) -> int:
    """Run a command, returning 1 for safe errors and 0 for committed success."""
    parser = argparse.ArgumentParser(prog="memory-admin")
    commands = parser.add_subparsers(dest="command", required=True)
    bootstrap = commands.add_parser("bootstrap", help="Create the first owner workspace")
    bootstrap.add_argument("--owner-name", required=True)
    bootstrap.add_argument("--workspace-name", default="default")
    scope = commands.add_parser("create-scope", help="Create a scope for the owner")
    scope.add_argument("--kind", choices=[kind.value for kind in ScopeKind], required=True)
    scope.add_argument("--name", required=True)
    commands.add_parser(
        "backfill-owner-permissions", help="Expand existing owner grants after rollout"
    )
    export = commands.add_parser("backup-export", help="Write an encrypted backup artifact")
    export.add_argument("--output", type=Path, required=True)
    export.add_argument("--storage-dir", type=Path, default=None)
    restore = commands.add_parser(
        "backup-restore", help="Restore an encrypted backup into an empty local database"
    )
    restore.add_argument("--input", type=Path, required=True)
    restore.add_argument("--storage-dir", type=Path, default=None)
    restore.add_argument(
        "--hmac-key-out",
        type=Path,
        required=True,
        help="Private file (created 0600) receiving the restored suppression key; never printed",
    )
    maintenance = commands.add_parser(
        "maintenance", help="Report retention work; use --apply to erase eligible text"
    )
    maintenance.add_argument("--apply", action="store_true")
    maintenance.add_argument("--session-retention-days", type=int, default=None)
    maintenance.add_argument("--event-retention-days", type=int, default=None)
    args = parser.parse_args(argv)
    engine: Engine | None = None
    try:
        settings = get_settings()
        url = settings.migration_database_url or settings.database_url
        engine = make_engine(url, prepare_threshold=settings.db_prepare_threshold)
        with engine.begin() as conn:
            if args.command == "bootstrap":
                owner_name = args.owner_name.strip()
                workspace_name = args.workspace_name.strip()
                if not owner_name or len(owner_name) > 100 or not workspace_name:
                    raise AppError(
                        ErrorCode.bad_request, "Owner and workspace names must be non-empty."
                    )
                workspace_id, token = bootstrap_workspace(
                    conn,
                    owner_name=owner_name,
                    workspace_name=workspace_name,
                )
                output = (
                    f"Workspace: {workspace_id}\nOwner token (store it now; shown once): {token}"
                )
            elif args.command == "backfill-owner-permissions":
                output = f"Expanded owner grants: {backfill_owner_permissions(conn)}"
            elif args.command == "maintenance":
                from memory_platform.services.maintenance import run_maintenance

                session_days = (
                    args.session_retention_days
                    if args.session_retention_days is not None
                    else settings.session_retention_days
                )
                event_days = (
                    args.event_retention_days
                    if args.event_retention_days is not None
                    else settings.event_retention_days
                )
                report = run_maintenance(
                    conn,
                    apply=args.apply,
                    session_retention_days=session_days,
                    event_retention_days=event_days,
                )
                output = (
                    f"Maintenance apply={report['apply']} "
                    f"expired_operations={report['expired_operations']} "
                    f"event_summaries={report['event_summaries']} "
                    f"sessions={report['sessions']}"
                )
            elif args.command in ("backup-export", "backup-restore"):
                from memory_platform.backup import export_backup, restore_backup

                storage_dir = Path(args.storage_dir) if args.storage_dir else settings.storage_dir
                if args.command == "backup-export":
                    summary = export_backup(
                        engine,
                        storage_dir,
                        args.output,
                        settings.memory_hmac_key.get_secret_value(),
                    )
                    tables = sum(summary["tables"].values())
                    output = (
                        f"Backup wrote {args.output}: "
                        f"{tables} rows, {summary['originals']} originals, "
                        f"{summary['encrypted_bytes']} encrypted bytes"
                    )
                else:
                    summary = restore_backup(engine, storage_dir, args.input)
                    key_path = Path(args.hmac_key_out)
                    if key_path.exists():
                        raise AppError(
                            ErrorCode.bad_request,
                            "Refusing to overwrite the existing HMAC key file.",
                        )
                    key_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                    fd = os.open(key_path, os.O_CREAT | os.O_WRONLY | os.O_EXCL, 0o600)
                    try:
                        with os.fdopen(fd, "w") as stream:
                            stream.write(summary["hmac_key_hex"])
                    except BaseException:
                        key_path.unlink(missing_ok=True)
                        raise
                    tables = sum(summary["tables"].values())
                    output = (
                        f"Restored {tables} rows and {summary['originals']} originals. "
                        f"Suppression key written to {key_path} (0600, not shown). "
                        f"Set MEMORY_HMAC_KEY to its contents before starting the API."
                    )
            else:
                result = create_scope(
                    conn,
                    _owner_principal(conn),
                    ScopeCreateRequest(kind=ScopeKind(args.kind), name=args.name),
                    request_id=f"admin:{uuid4()}",
                )
                output = f"Scope: {result.id} ({result.name})"
        print(output)
        return 0
    except AppError as exc:
        print(exc.message, file=sys.stderr)
        return 1
    except Exception:
        # Driver and settings errors can contain credentials. Never echo their text.
        print(
            "Admin command failed. Check configuration and database availability.", file=sys.stderr
        )
        return 1
    finally:
        if engine is not None:
            engine.dispose()


if __name__ == "__main__":
    raise SystemExit(main())
