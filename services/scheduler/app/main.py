"""MiniCloud Scheduler — manages workflow execution schedules via cron expressions.
Supports:
- Named schedules (templates created by admin)
- Ad-hoc schedules (one-time schedules using named schedule or custom cron)
"""
import logging
import os
import uuid
from typing import Annotated

import httpx
from apscheduler.schedulers.background import BackgroundScheduler
from fastapi import Depends, FastAPI, HTTPException, Header
from pydantic import BaseModel, Field

LOG = logging.getLogger("scheduler")
logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"))

ORCHESTRATOR_URL = os.environ.get("ORCHESTRATOR_URL", "http://localhost:8083").rstrip("/")
SCHEDULER_ADMIN_USERS = os.environ.get("SCHEDULER_ADMIN_USERS", "admin").strip().split(",")
SCHEDULER_CONTRIBUTOR_USERS = os.environ.get("SCHEDULER_CONTRIBUTOR_USERS", "operator").strip().split(",")

app = FastAPI(title="MiniCloud Scheduler", version="0.1.0")
scheduler = BackgroundScheduler()

# Storage
_schedules = {}  # {job_id: {"workflow": name, "cron": expr, "payload": xml}}
_named_schedules = {}  # {id: {"name": str, "cron_expression": str, "description": str}}


# ============================================================================
# Models
# ============================================================================
class NamedScheduleRequest(BaseModel):
    """Create/update a named schedule template."""
    name: str = Field(..., min_length=1, description="E.g., 'every-midnight', 'every-friday'")
    cron_expression: str = Field(..., min_length=3, description="Cron: '0 0 * * *'")
    description: str = Field(default="", description="Human-readable description")


class NamedScheduleResponse(BaseModel):
    """Named schedule template info."""
    id: str
    name: str
    cron_expression: str
    description: str


class ScheduleRequest(BaseModel):
    """Create/update a scheduled workflow (use named schedule OR custom cron)."""
    workflow_name: str = Field(..., min_length=1)
    cron_expression: str | None = Field(default=None, description="Custom cron or None to use named_schedule_id")
    named_schedule_id: str | None = Field(default=None, description="Reference to named schedule")
    payload: str = Field(default='<root/>', description="XML payload to send on trigger")


class ScheduleResponse(BaseModel):
    """Scheduled workflow info."""
    job_id: str
    workflow_name: str
    cron_expression: str
    payload: str
    next_run_time: str | None = None


# ============================================================================
# Auth helpers
# ============================================================================
async def _verify_scheduler_permission(
    request,
    x_user: Annotated[str | None, Header(alias="X-User")] = None,
) -> str:
    """Verify user has scheduler admin or contributor rights."""
    user = x_user or "anonymous"
    allowed_users = SCHEDULER_ADMIN_USERS + SCHEDULER_CONTRIBUTOR_USERS
    if user not in allowed_users:
        raise HTTPException(
            status_code=403,
            detail=f"User '{user}' not authorized to manage schedules",
        )
    return user


async def _verify_scheduler_admin(
    x_user: Annotated[str | None, Header(alias="X-User")] = None,
) -> str:
    """Verify user is admin for schedule/template deletion/editing."""
    user = x_user or "anonymous"
    if user not in SCHEDULER_ADMIN_USERS:
        raise HTTPException(
            status_code=403,
            detail=f"User '{user}' must be admin to modify schedules",
        )
    return user


# ============================================================================
# Scheduler background job
# ============================================================================
async def _trigger_workflow(workflow_name: str, payload: str) -> None:
    """Execute workflow via orchestrator /invoke/scheduled endpoint."""
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(
                f"{ORCHESTRATOR_URL}/invoke/scheduled",
                json={"workflow": workflow_name, "xml": payload},
            )
            if resp.status_code >= 400:
                LOG.error(
                    "trigger failed: workflow=%s status=%d response=%s",
                    workflow_name,
                    resp.status_code,
                    resp.text,
                )
            else:
                LOG.info("triggered workflow=%s via schedule", workflow_name)
    except Exception as e:
        LOG.error("trigger exception: workflow=%s error=%s", workflow_name, e)


# ============================================================================
# Routes
# ============================================================================
@app.get("/healthz")
def healthz() -> dict:
    return {"status": "ok"}


@app.get("/schedules")
def list_schedules() -> list[ScheduleResponse]:
    """List all scheduled workflows."""
    result = []
    for job_id, sched in _schedules.items():
        job = scheduler.get_job(job_id)
        result.append(
            ScheduleResponse(
                job_id=job_id,
                workflow_name=sched["workflow"],
                cron_expression=sched["cron"],
                payload=sched["payload"],
                next_run_time=str(job.next_run_time) if job else None,
            )
        )
    return result


@app.post("/schedules")
async def create_schedule(
    body: ScheduleRequest,
    user: str = Depends(_verify_scheduler_permission),
) -> ScheduleResponse:
    """Create a new workflow schedule (using named schedule OR custom cron)."""
    from apscheduler.triggers.cron import CronTrigger

    # Determine cron expression
    cron_expr = None
    if body.cron_expression:
        cron_expr = body.cron_expression
    elif body.named_schedule_id:
        if body.named_schedule_id not in _named_schedules:
            raise HTTPException(status_code=404, detail=f"Named schedule not found: {body.named_schedule_id}")
        cron_expr = _named_schedules[body.named_schedule_id]["cron_expression"]
    else:
        raise HTTPException(
            status_code=400, 
            detail="Either cron_expression or named_schedule_id must be provided"
        )

    # Validate cron expression
    try:
        CronTrigger.from_crontab(cron_expr)
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Invalid cron expression: {e}")

    job_id = str(uuid.uuid4())
    _schedules[job_id] = {
        "workflow": body.workflow_name,
        "cron": cron_expr,
        "payload": body.payload,
        "named_schedule_id": body.named_schedule_id,
    }

    # Schedule the job
    job = scheduler.add_job(
        _trigger_workflow,
        "cron",
        args=(body.workflow_name, body.payload),
        id=job_id,
        replace_existing=True,
        **_parse_cron(cron_expr),
    )

    LOG.info(
        "schedule created: job_id=%s workflow=%s cron=%s named_schedule_id=%s user=%s",
        job_id,
        body.workflow_name,
        cron_expr,
        body.named_schedule_id,
        user,
    )

    return ScheduleResponse(
        job_id=job_id,
        workflow_name=body.workflow_name,
        cron_expression=cron_expr,
        payload=body.payload,
        next_run_time=str(job.next_run_time),
    )


@app.delete("/schedules/{job_id}")
async def delete_schedule(
    job_id: str,
    user: str = Depends(_verify_scheduler_admin),
) -> dict:
    """Delete a scheduled workflow (admin only)."""
    if job_id not in _schedules:
        raise HTTPException(status_code=404, detail="Schedule not found")

    scheduler.remove_job(job_id)
    del _schedules[job_id]

    LOG.info("schedule deleted: job_id=%s user=%s", job_id, user)
    return {"status": "deleted", "job_id": job_id}


# ============================================================================
# Named Schedule Management (Admin Only)
# ============================================================================
@app.get("/named-schedules")
def list_named_schedules() -> list[NamedScheduleResponse]:
    """List all named schedule templates (public, no auth required)."""
    result = []
    for sid, data in _named_schedules.items():
        result.append(
            NamedScheduleResponse(
                id=sid,
                name=data["name"],
                cron_expression=data["cron_expression"],
                description=data["description"],
            )
        )
    return result


@app.post("/named-schedules")
async def create_named_schedule(
    body: NamedScheduleRequest,
    user: str = Depends(_verify_scheduler_admin),
) -> NamedScheduleResponse:
    """Create a named schedule template (admin only)."""
    from apscheduler.triggers.cron import CronTrigger

    # Validate cron expression
    try:
        CronTrigger.from_crontab(body.cron_expression)
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Invalid cron expression: {e}")

    # Check for duplicate names
    if any(s["name"] == body.name for s in _named_schedules.values()):
        raise HTTPException(status_code=409, detail=f"Named schedule with name '{body.name}' already exists")

    schedule_id = str(uuid.uuid4())
    _named_schedules[schedule_id] = {
        "name": body.name,
        "cron_expression": body.cron_expression,
        "description": body.description,
    }

    LOG.info(
        "named schedule created: id=%s name=%s cron=%s user=%s",
        schedule_id,
        body.name,
        body.cron_expression,
        user,
    )

    return NamedScheduleResponse(
        id=schedule_id,
        name=body.name,
        cron_expression=body.cron_expression,
        description=body.description,
    )


@app.get("/named-schedules/{schedule_id}")
def get_named_schedule(schedule_id: str) -> NamedScheduleResponse:
    """Get a named schedule template by ID."""
    if schedule_id not in _named_schedules:
        raise HTTPException(status_code=404, detail="Named schedule not found")

    data = _named_schedules[schedule_id]
    return NamedScheduleResponse(
        id=schedule_id,
        name=data["name"],
        cron_expression=data["cron_expression"],
        description=data["description"],
    )


@app.put("/named-schedules/{schedule_id}")
async def update_named_schedule(
    schedule_id: str,
    body: NamedScheduleRequest,
    user: str = Depends(_verify_scheduler_admin),
) -> NamedScheduleResponse:
    """Update a named schedule template (admin only)."""
    from apscheduler.triggers.cron import CronTrigger

    if schedule_id not in _named_schedules:
        raise HTTPException(status_code=404, detail="Named schedule not found")

    # Validate cron expression
    try:
        CronTrigger.from_crontab(body.cron_expression)
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Invalid cron expression: {e}")

    # Check for duplicate names (excluding self)
    if any(
        s["name"] == body.name and sid != schedule_id
        for sid, s in _named_schedules.items()
    ):
        raise HTTPException(status_code=409, detail=f"Named schedule with name '{body.name}' already exists")

    _named_schedules[schedule_id] = {
        "name": body.name,
        "cron_expression": body.cron_expression,
        "description": body.description,
    }

    LOG.info(
        "named schedule updated: id=%s name=%s cron=%s user=%s",
        schedule_id,
        body.name,
        body.cron_expression,
        user,
    )

    return NamedScheduleResponse(
        id=schedule_id,
        name=body.name,
        cron_expression=body.cron_expression,
        description=body.description,
    )


@app.delete("/named-schedules/{schedule_id}")
async def delete_named_schedule(
    schedule_id: str,
    user: str = Depends(_verify_scheduler_admin),
) -> dict:
    """Delete a named schedule template (admin only)."""
    if schedule_id not in _named_schedules:
        raise HTTPException(status_code=404, detail="Named schedule not found")

    # Check if any active schedules are using this named schedule
    using_count = sum(
        1 for s in _schedules.values()
        if s.get("named_schedule_id") == schedule_id
    )

    if using_count > 0:
        raise HTTPException(
            status_code=409,
            detail=f"Cannot delete named schedule: {using_count} active schedule(s) are using it",
        )

    del _named_schedules[schedule_id]

    LOG.info("named schedule deleted: id=%s user=%s", schedule_id, user)
    return {"status": "deleted", "schedule_id": schedule_id}



# ============================================================================
# Startup/Shutdown
# ============================================================================
@app.on_event("startup")
async def startup():
    scheduler.start()
    LOG.info("scheduler started")


@app.on_event("shutdown")
async def shutdown():
    scheduler.shutdown()
    LOG.info("scheduler shutdown")


# ============================================================================
# Helpers
# ============================================================================
def _parse_cron(cron_expr: str) -> dict:
    """Parse cron expression into apscheduler cron kwargs."""
    parts = cron_expr.strip().split()
    if len(parts) != 5:
        raise ValueError("Cron must be: minute hour day month weekday")
    minute, hour, day, month, weekday = parts
    return {
        "minute": minute,
        "hour": hour,
        "day": day,
        "month": month,
        "day_of_week": weekday,
    }
