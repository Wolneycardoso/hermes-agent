"""Execution gate for Kanban tasks with a local-only completion contract.

The dispatcher marks these workers with ``HERMES_KANBAN_EFFECTS=blocked``.
This module is deliberately fail-closed for tool schemas: only the local
coding/research surface and Kanban lifecycle tools remain available. The
terminal boundary adds a second, command-level guard for explicit publication,
deployment, VCS integration, infrastructure mutation, and messaging commands.
"""

from __future__ import annotations

import os
import re
from typing import Any, Optional

_EFFECTS_ENV = "HERMES_KANBAN_EFFECTS"
_BLOCKED_VALUE = "blocked"

# Keep this allowlist small and explicit. New tools are blocked until their
# local-only safety is reviewed, rather than silently becoming available.
_LOCAL_ONLY_ALLOWED_TOOLS = frozenset({
    "web_search", "web_extract", "vision_analyze",
    "read_file", "write_file", "patch", "search_files",
    "skills_list", "skill_view", "skill_manage",
    "todo_list",
    "kanban_show", "kanban_list", "kanban_complete", "kanban_block",
    "kanban_request_review", "kanban_request_changes", "kanban_heartbeat",
    "kanban_comment", "kanban_create", "kanban_link", "kanban_unblock",
    "kanban_attach", "kanban_attach_url", "kanban_attachments", "kanban_schedule",
})

# These commands are external effects even when they are issued through the
# terminal rather than through a first-class connector. Read-only git commands
# and local builds/tests remain available.
_EXTERNAL_COMMAND_PATTERNS = tuple(re.compile(pattern, re.IGNORECASE) for pattern in (
    r"\bgit\s+(?:push|merge|rebase|remote\s+(?:add|remove|set-url))\b",
    r"\bgh\s+(?:pr|issue|release|repo|gist)\s+(?:create|merge|edit|close|comment|review|upload|delete|publish)\b",
    r"\b(?:vercel|netlify|flyctl|railway|render|heroku)\s+(?:deploy|promote|rollback|destroy|delete)\b",
    r"(?:deployer/vercel-runtime|vercel-runtime)\s+(?:stage|deploy|promote|rollback|destroy|delete)\b",
    r"\bsupabase\s+(?:db\s+push|migration\s+(?:up|repair|new)|functions\s+deploy)\b",
    r"\b(?:npm|pnpm|yarn|bun|pip|uv|poetry|twine)\s+(?:publish|upload)\b",
    r"\b(?:docker|podman)\s+push\b",
    r"\b(?:terraform|tofu)\s+(?:apply|destroy|import)\b",
    r"\b(?:kubectl|helm)\s+(?:apply|delete|patch|rollout|scale|set|install|upgrade|uninstall)\b",
    r"\b(?:systemctl|launchctl|service)\s+(?:start|stop|restart|enable|disable|reload)\b",
    r"\b(?:kill|pkill|killall|sudo|doas|su)\b",
    r"\bhermes\s+gateway\s+(?:start|stop|restart|uninstall)\b",
    r"\b(?:aws|gcloud|az)\s+(?:deploy|create|update|delete|put|publish|release)\b",
    r"\b(?:ssh|scp|rsync|nc|socat|sendmail|mutt)\b",
    r"\b(?:curl|wget)\b",
))

# A local-only worker may inspect and compile code, but an executable test/build
# runner can execute repository-controlled code with subprocess, network,
# filesystem, or deployment effects. Reject those forms before the narrower
# publication patterns below. The terminal tool remains usable for simple
# commands such as ``python -m py_compile`` and ``node --check``.
_LOCAL_ONLY_UNSAFE_SYNTAX = tuple(re.compile(pattern, re.IGNORECASE) for pattern in (
    r"[;&|]", r"[\r\n]", r"`", r"\$\(", r"[<>]{1,2}",
    r"(?:^|[\s;|])(?:bash|sh|dash|zsh|fish|ksh|pwsh|powershell|cmd)(?:\s|$)",
    r"(?:^|[\s;|])(?:env|xargs|eval|exec|make|just|task)(?:\s|$)",
    r"(?:^|[\s;|])(?:pytest|py\.test|nose|nosetests|unittest|tox|nox|coverage|jest|vitest|mocha|rspec)(?:\s|$)",
    r"(?:^|[\s;|])(?:npm|pnpm|yarn|bun)(?:\s|$)",
    r"(?:^|[\s;|])(?:cargo\s+test|go\s+test|mvn\s+test|gradle\s+test|bundle\s+exec)(?:\s|$)",
    r"\b(?:python|python3|pypy|node|nodejs|perl|ruby|php)\s+(?:-c|-e|--eval|--print|-r)\b",
    r"(?:^|[\s;|])(?:python|python3|pypy)(?:\s|$)",
    r"(?:^|[\s;|])(?:node|nodejs|perl|ruby|php)(?:\s|$)",
    r"\bpython(?:3)?\s+-m\s+(?:http\.server|socket|smtplib|imaplib|ftplib)\b",
    r"\b(?:pip|uv)\s+(?:install|add|sync)\b",
))

_LOCAL_ONLY_SAFE_SYNTAX = tuple(re.compile(pattern, re.IGNORECASE) for pattern in (
    r"^\s*(?:python|python3|pypy)\s+-m\s+(?:py_compile|compileall)(?:\s+[^;&|<>`$]+)?\s*$",
    r"^\s*(?:python|python3|pypy)\s+(?:-V|--version)\s*$",
    r"^\s*(?:node|nodejs)\s+--check\s+[^;&|<>`$]+\s*$",
    r"^\s*(?:node|nodejs)\s+(?:-v|--version)\s*$",
    r"^\s*(?:perl)\s+-c\s+[^;&|<>`$]+\s*$",
    r"^\s*(?:ruby)\s+-c\s+[^;&|<>`$]+\s*$",
    r"^\s*(?:php)\s+-l\s+[^;&|<>`$]+\s*$",
))


def local_only_active() -> bool:
    """Return whether the current process is executing a local-only task."""
    return os.environ.get(_EFFECTS_ENV, "").strip().lower() == _BLOCKED_VALUE


def local_only_tool_allowed(tool_name: str) -> bool:
    """Return whether *tool_name* is in the reviewed local-only surface."""
    name = str(tool_name or "").strip()
    return name in _LOCAL_ONLY_ALLOWED_TOOLS


def blocked_tool_message(tool_name: str, _args: Optional[dict[str, Any]] = None) -> Optional[str]:
    """Return a deterministic block message, or ``None`` when the call may run."""
    if not local_only_active() or local_only_tool_allowed(tool_name):
        return None
    return (
        "Blocked: this Kanban mission has a local-only completion contract. "
        f"Tool {tool_name!r} is not available because it may cause an external effect; "
        "request explicit authorization in a separate mission."
    )


def local_only_terminal_block(command: str) -> Optional[str]:
    """Fail closed until a real OS isolation backend is proven and wired.

    This is intentionally command-independent: command inspection cannot form a
    security boundary because scripts and alternate executables can execute
    arbitrary code. The regex tables below are retained only for compatibility
    with callers/tests outside the local-only path; they are never consulted
    while the capability is unproven.
    """
    if not local_only_active() or not isinstance(command, str):
        return None
    return (
        "Blocked: this Kanban mission requires a proven OS isolation backend for "
        "local-only execution; no such backend is integrated, so terminal execution "
        "is disabled without fallback to the host."
    )


__all__ = [
    "blocked_tool_message", "local_only_active", "local_only_terminal_block",
    "local_only_tool_allowed",
]
