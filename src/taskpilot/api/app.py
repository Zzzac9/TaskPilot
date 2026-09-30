"""FastAPI 路由：只负责 HTTP 映射，不复制 Graph orchestration。"""

from contextlib import asynccontextmanager
from typing import AsyncIterator, cast

from fastapi import FastAPI, HTTPException, Request, status

from taskpilot.api.schemas import (
    CreateTaskRequest,
    InterruptView,
    PlanStepView,
    RecoveryResumeRequest,
    ResumeTaskRequest,
    TaskView,
    ToolApprovalResumeRequest,
    TraceSummaryView,
)
from taskpilot.models import TaskStatus
from taskpilot.service import (
    TaskConfigurationError,
    TaskConflictError,
    TaskNotFoundError,
    TaskPilotService,
    TaskRunSnapshot,
)


def create_app(service: TaskPilotService | None = None) -> FastAPI:
    """构造可注入 Service 的 app；默认仅面向本地开发运行。"""

    active_service = service or TaskPilotService()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        async with active_service:
            app.state.taskpilot_service = active_service
            yield

    app = FastAPI(title="TaskPilot", version="0.1.0", lifespan=lifespan)

    @app.post("/tasks", response_model=TaskView)
    async def create_task(payload: CreateTaskRequest, request: Request) -> TaskView:
        runtime = _service(request)
        try:
            snapshot = await runtime.run_new_task(
                payload.task,
                enable_browser=payload.enable_browser,
            )
        except TaskConfigurationError as exc:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)
            ) from exc
        return _task_view(snapshot)

    @app.post("/tasks/{task_id}/resume", response_model=TaskView)
    async def resume_task(
        task_id: str,
        payload: ResumeTaskRequest,
        request: Request,
    ) -> TaskView:
        runtime = _service(request)
        if isinstance(payload, ToolApprovalResumeRequest):
            interrupt_type = payload.type
            response = payload.model_dump(exclude={"type"}, exclude_none=True)
        else:
            recovery = cast(RecoveryResumeRequest, payload)
            interrupt_type = recovery.type
            response = recovery.model_dump(exclude={"type"}, exclude_none=True)
        try:
            snapshot = await runtime.resume_task(
                task_id,
                interrupt_type=interrupt_type,
                response=response,
            )
        except TaskNotFoundError as exc:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)
            ) from exc
        except TaskConflictError as exc:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT, detail=str(exc)
            ) from exc
        except TaskConfigurationError as exc:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)
            ) from exc
        return _task_view(snapshot)

    @app.get("/tasks/{task_id}", response_model=TaskView)
    async def get_task(task_id: str, request: Request) -> TaskView:
        try:
            snapshot = await _service(request).get_task(task_id)
        except TaskNotFoundError as exc:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)
            ) from exc
        return _task_view(snapshot)

    @app.get(
        "/tasks/{task_id}/trace-summary",
        response_model=TraceSummaryView,
    )
    async def get_trace_summary(task_id: str, request: Request) -> TraceSummaryView:
        try:
            summary = await _service(request).get_trace_summary(task_id)
        except TaskNotFoundError as exc:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)
            ) from exc
        return TraceSummaryView.model_validate(summary.model_dump())

    return app


def _service(request: Request) -> TaskPilotService:
    return cast(TaskPilotService, request.app.state.taskpilot_service)


def _task_view(snapshot: TaskRunSnapshot) -> TaskView:
    """显式裁剪内部 State；PendingToolAction raw arguments 永远不会外泄。"""

    state = snapshot.state
    task_spec = state.get("task_spec")
    goal = getattr(task_spec, "goal", None)
    steps = []
    for step in state.get("plan", []):
        step_status = (
            step.status.value if hasattr(step.status, "value") else str(step.status)
        )
        steps.append(
            PlanStepView(
                id=step.id,
                description=step.description,
                status=step_status,
                depends_on=list(step.depends_on),
                success_criteria=list(step.success_criteria),
            )
        )
    raw_status = state.get("status")
    status_value = (
        raw_status.value if isinstance(raw_status, TaskStatus) else str(raw_status)
    )
    interrupt = None
    if snapshot.interrupt is not None:
        raw = snapshot.interrupt
        preview = raw.get("arguments_preview")
        interrupt = InterruptView(
            type=str(raw.get("type") or "unknown"),
            tool_name=(
                str(raw["tool_name"]) if raw.get("tool_name") is not None else None
            ),
            risk_level=(
                str(raw["risk_level"]) if raw.get("risk_level") is not None else None
            ),
            reason=(str(raw["reason"]) if raw.get("reason") is not None else None),
            arguments_preview=(dict(preview) if isinstance(preview, dict) else None),
            choices=(
                list(raw["choices"]) if isinstance(raw.get("choices"), list) else None
            ),
        )
    return TaskView(
        task_id=str(state["task_id"]),
        status=status_value,
        goal=goal,
        current_step_id=state.get("current_step_id"),
        plan_revision=int(state.get("plan_revision", 0)),
        replan_count=int(state.get("replan_count", 0)),
        plan=steps,
        interrupt=interrupt,
        final_answer=state.get("final_answer"),
        error=state.get("error"),
    )
