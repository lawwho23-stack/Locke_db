"""Daily recovery uses a separate deployment secret, never an owner token."""

import secrets
from typing import Any

from fastapi import APIRouter, Request

from memory_platform.api.deps import EngineDep, SettingsDep
from memory_platform.errors import AppError, ErrorCode
from memory_platform.services.hosted_processing import process_pending

router = APIRouter()


@router.get("/internal/cron")
def daily_recovery(request: Request, engine: EngineDep, settings: SettingsDep) -> dict[str, Any]:
    secret = settings.cron_secret.get_secret_value() if settings.cron_secret else ""
    if len(secret) < 32 or not secrets.compare_digest(
        request.headers.get("authorization", ""), "Bearer " + secret
    ):
        raise AppError(ErrorCode.unauthenticated, "Invalid recovery credentials.")
    if settings.cron_workspace_id is None:
        raise AppError(ErrorCode.dependency_unavailable, "Recovery workspace is not configured.")
    return process_pending(engine, settings, settings.cron_workspace_id)
