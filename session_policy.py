"""
Whether to pass a stored session id to Hermes / Cursor agent CLI, and when to persist
new session ids. Env:

  HERMES_SESSION_POLICY — new | resume (default) | resume_ttl
  HERMES_SESSION_TTL_S — used only for resume_ttl; default 1800 (30m)

  HERMES_CURSOR_RESUME_FLAG — flag before session id, default: --resume
  (If your build uses a different flag, set this to one token, e.g. --continue
   if your agent expects that name — value pairing must still be two args unless
   you switch to a single argv via bridge extension.)

  HERMES_SESSION_PERSIST — when false, never write ~/.cache/vexa-bridge/<alias>.session
  (default: true, except HERMES_SESSION_POLICY=new does not persist by design)

  HERMES_CURSOR_CONTINUE_ONLY — if true, agent gets only a bare --continue (or
  HERMES_CURSOR_BARE_FLAG) when a stored session is valid; the id is not passed
  on the command line. Default is false: use HERMES_CURSOR_RESUME_FLAG + id.
"""
from __future__ import annotations

import os
import time
POLICY_NEW = "new"
POLICY_RESUME = "resume"
POLICY_RESUME_TTL = "resume_ttl"
DEFAULT_TTL_S = 1800.0


def _policy() -> str:
    return os.environ.get("HERMES_SESSION_POLICY", POLICY_RESUME).strip().lower()


def _ttl_s() -> float:
    raw = os.environ.get("HERMES_SESSION_TTL_S", str(int(DEFAULT_TTL_S)))
    try:
        return max(0.0, float(raw))
    except ValueError:
        return DEFAULT_TTL_S


def _persist_enabled() -> bool:
    v = os.environ.get("HERMES_SESSION_PERSIST", "true").strip().lower()
    return v in ("1", "true", "yes", "on")


def cursor_resume_flag() -> str:
    return (os.environ.get("HERMES_CURSOR_RESUME_FLAG", "--resume") or "--resume").strip()


def continue_only() -> bool:
    """If true, agent CLI only gets a bare flag (default --continue), no id pair."""
    v = os.environ.get("HERMES_CURSOR_CONTINUE_ONLY", "").strip().lower()
    return v in ("1", "true", "yes", "on")


def bare_continue_flag() -> str:
    return (os.environ.get("HERMES_CURSOR_BARE_FLAG", "--continue") or "--continue").strip()


def should_persist_captured_session() -> bool:
    if not _persist_enabled():
        return False
    if _policy() == POLICY_NEW:
        return False
    return True


def resolve_resume_id(state_path: str) -> tuple[str | None, str]:
    """
    Decide the session id to pass to the CLI, if any. Also applies policy= new
    by removing the state file.
    """
    p = (state_path or "").strip()
    pol = _policy()
    if pol not in (POLICY_NEW, POLICY_RESUME, POLICY_RESUME_TTL):
        return None, f"invalid_HERMES_SESSION_POLICY={pol!r}"

    if pol == POLICY_NEW:
        if p and _safe_exists(p):
            try:
                os.remove(p)
            except OSError as e:
                return None, f"policy_new_clear_failed:{e!r}"
        return None, "policy_new"

    if not p or not _safe_exists(p):
        return None, f"policy={pol} no_stored_session"

    try:
        with open(p, encoding="utf-8") as f:
            sid = f.read().strip() or None
    except OSError as e:
        return None, f"read_error:{e!r}"
    if not sid:
        return None, f"policy={pol} empty_file"

    if pol == POLICY_RESUME:
        return sid, "policy_resume"

    mtime = _safe_mtime(p)
    if mtime is None:
        return None, "policy_resume_ttl no_mtime"
    if time.time() - mtime > _ttl_s():
        try:
            os.remove(p)
        except OSError:
            pass
        return None, f"policy_resume_ttl expired ttl_s={_ttl_s():.0f}"

    return sid, f"policy_resume_ttl age_s={time.time() - mtime:.0f}"


def cursor_cli_resume_fragment(session_id: str | None) -> list[str]:
    """Argv after agent binary, before -p, when a resume id is available."""
    s = (session_id or "").strip()
    if not s:
        return []
    if continue_only():
        f = bare_continue_flag()
        if not f.startswith("-"):
            f = f"--{f}"
        return [f]
    flag = cursor_resume_flag()
    if not flag.startswith("-"):
        flag = f"--{flag}"
    return [flag, s]


def _safe_exists(path: str) -> bool:
    try:
        return os.path.isfile(path)
    except OSError:
        return False


def _safe_mtime(path: str) -> float | None:
    try:
        return os.path.getmtime(path)
    except OSError:
        return None
