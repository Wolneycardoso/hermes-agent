"""Shared natural-language mission intake for Kanban surfaces.

The parser intentionally does not try to solve the mission. It stores the user's
request as a durable triage card so the existing specifier/decomposer/dispatcher
pipeline remains the single routing path.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import re
import sqlite3
from typing import Iterable, Optional, cast

from hermes_cli import kanban_db as kb


_MAX_REQUEST_LENGTH = 16_000
_MAX_TITLE_LENGTH = 240
_LEADING_IMPERATIVE = re.compile(
    r"^(?:crie|criar|create|pedir|pedido|por\s+favor[,\s]*)\s+",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class NaturalMission:
    """Normalized mission fields before persistence."""

    request: str
    title: str
    body: str
    idempotency_key: str


def normalize_request(request: str) -> str:
    """Collapse chat whitespace and remove a leading creation imperative."""
    if not isinstance(request, str):
        raise ValueError("natural request must be text")
    normalized = " ".join(request.split())
    normalized = _LEADING_IMPERATIVE.sub("", normalized, count=1).strip()
    if not normalized:
        raise ValueError("natural request is empty")
    if len(normalized) > _MAX_REQUEST_LENGTH:
        raise ValueError(f"natural request is too long (max {_MAX_REQUEST_LENGTH} characters)")
    return normalized


def _title_for(request: str) -> str:
    title = request.splitlines()[0].strip()
    if len(title) <= _MAX_TITLE_LENGTH:
        return title
    return title[: _MAX_TITLE_LENGTH - 1].rstrip() + "…"


def _body_for(request: str) -> str:
    return (
        "Intenção natural recebida por /kanban ou pelo Dashboard:\n\n"
        f"{request}\n\n"
        "Contrato de execução:\n"
        "- manter esta missão em triage para especificação e decomposição;\n"
        "- usar o dispatcher e os perfis existentes, sem criar uma fila paralela;\n"
        "- decompor em tarefas filhas, dependências e critérios de aceite verificáveis;\n"
        "- preservar o pedido original e registrar lacunas como bloqueios;\n"
        "- deploy, merge, publicação, migrations, produção e mensagens externas "
        "ficam bloqueados/pendentes de autorização explícita."
    )


def prepare_mission(
    request: str,
    *,
    board: Optional[str] = None,
    idempotency_key: Optional[str] = None,
) -> NaturalMission:
    """Return deterministic fields without changing the board."""
    normalized = normalize_request(request)
    digest_source = f"{board or kb.get_current_board()}\0{normalized}".encode("utf-8")
    key = idempotency_key or f"natural:{hashlib.sha256(digest_source).hexdigest()[:32]}"
    return NaturalMission(
        request=normalized,
        title=_title_for(normalized),
        body=_body_for(normalized),
        idempotency_key=key,
    )


def create_mission(
    conn: sqlite3.Connection,
    request: str,
    *,
    board: Optional[str] = None,
    assignee: Optional[str] = None,
    created_by: Optional[str] = None,
    workspace_kind: Optional[str] = None,
    workspace_path: Optional[str] = None,
    branch_name: Optional[str] = None,
    project_id: Optional[str] = None,
    tenant: Optional[str] = None,
    priority: int = 0,
    skills: Optional[Iterable[str]] = None,
    max_retries: Optional[int] = None,
    max_runtime_seconds: Optional[int] = None,
    model_override: Optional[str] = None,
    provider_override: Optional[str] = None,
    goal_mode: bool = False,
    goal_max_turns: Optional[int] = None,
    session_id: Optional[str] = None,
    idempotency_key: Optional[str] = None,
) -> tuple[kb.Task, bool, NaturalMission]:
    """Create an idempotent triage mission and return ``(task, created, spec)``."""
    spec = prepare_mission(request, board=board, idempotency_key=idempotency_key)
    task_id, created = cast(tuple[str, bool], kb.create_task(
        conn,
        title=spec.title,
        body=spec.body,
        assignee=assignee,
        created_by=created_by,
        workspace_kind=workspace_kind,
        workspace_path=workspace_path,
        branch_name=branch_name,
        project_id=project_id,
        tenant=tenant,
        priority=priority,
        triage=True,
        idempotency_key=spec.idempotency_key,
        max_retries=max_retries,
        max_runtime_seconds=max_runtime_seconds,
        skills=skills,
        model_override=model_override,
        provider_override=provider_override,
        goal_mode=goal_mode,
        goal_max_turns=goal_max_turns,
        session_id=session_id,
        completion_contract="local-only",
        isolation_policy="natural-local-only",
        board=board,
        return_created=True,
    ))
    task = kb.get_task(conn, task_id)
    if task is None:
        raise RuntimeError(f"created Kanban task {task_id} could not be read back")
    return task, created, spec
