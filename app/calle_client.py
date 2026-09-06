import json
import logging
import os
import shutil
import subprocess

logger = logging.getLogger(__name__)

TERMINAL_STATUSES = {
    "COMPLETED",
    "FAILED",
    "NO_ANSWER",
    "DECLINED",
    "CANCELLED",
    "VOICEMAIL",
    "BUSY",
    "EXPIRED",
}


def _normalize_status(status):
    """Canonicalise a CALL-E status string so the same logical status
    doesn't get two different keys in TERMINAL_STATUSES or in the
    failure-text / friendly-status dicts. CALL-E has been observed
    returning both 'NO_ANSWER' (underscore) and 'NO ANSWER' (space)
    for the same outcome — pick one and stick with it.
    """
    if not status:
        return ""
    return str(status).strip().upper().replace(" ", "_")


def normalize_status(status):
    """Public wrapper. Use everywhere a CALL-E status string is used
    as a dict key, persisted to the DB, or compared against
    TERMINAL_STATUSES.
    """
    return _normalize_status(status)


def is_terminal(status):
    """True iff the (raw) CALL-E status is a known terminal status.
    Normalises spacing/case before lookup so 'NO ANSWER', 'no_answer',
    'No Answer' all match the same set entry.
    """
    return _normalize_status(status) in TERMINAL_STATUSES


DEFAULT_TIMEOUTS = {"plan_call": 200, "run_call": 120, "get_call_run": 120}
_SENSITIVE_KEYS = {"confirm_token"}
_INTEGRATION_ENV = {
    "CALLE_SOURCE": "skills_sh",
    "CALLE_INTEGRATION": "skills_sh_skill",
    "CALLE_INTEGRATION_VERSION": "0.1.0",
}


class CalleError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        returncode: int | None = None,
        stderr: str | None = None,
    ) -> None:
        super().__init__(message)
        self.returncode = returncode
        self.stderr = stderr


class CalleAuthError(CalleError):
    pass


def redact(obj):
    if isinstance(obj, dict):
        return {
            k: (
                f"<redacted len={len(v)}>"
                if k in _SENSITIVE_KEYS and isinstance(v, str)
                else redact(v)
            )
            for k, v in obj.items()
        }
    if isinstance(obj, list):
        return [redact(item) for item in obj]
    return obj


def _binary() -> str:
    path = shutil.which("calle")
    if not path:
        raise CalleError(
            "calle CLI not found on PATH. Install the official calle CLI and "
            "set CALLE_SOURCE / CALLE_INTEGRATION / CALLE_INTEGRATION_VERSION "
            "before running."
        )
    return path


def _check_payload(payload: object, tool: str) -> None:
    if not isinstance(payload, dict):
        return
    if payload.get("ok") is False:
        error = payload.get("error") or {}
        if error.get("code") == "auth_required":
            raise CalleAuthError(
                f"CALL-E authentication required while calling {tool}. "
                "Run `calle auth login` and retry."
            )
        raise CalleError(f"calle {tool} failed: {error or payload}")
    result = payload.get("result")
    if isinstance(result, dict) and result.get("isError"):
        text = "; ".join(
            block.get("text", "")
            for block in result.get("content", [])
            if isinstance(block, dict)
        )
        raise CalleError(f"calle {tool} returned a tool error: {text or result}")


def _invoke_tool(tool: str, args: dict, timeout: float) -> dict:
    cmd = [_binary(), "mcp", "call", tool, "--args-json", json.dumps(args), "--json"]
    logger.info(
        "calle subprocess start: tool=%s timeout=%ss args=%s",
        tool,
        timeout,
        redact(args),
    )
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            env={**os.environ, **_INTEGRATION_ENV},
        )
    except subprocess.TimeoutExpired as exc:
        logger.error("calle %s timed out after %ss", tool, timeout)
        raise CalleError(f"calle {tool} timed out after {timeout}s") from exc
    except OSError as exc:
        raise CalleError(f"failed to execute calle binary: {exc}") from exc

    if proc.returncode != 0:
        detail = proc.stderr.strip() or proc.stdout.strip()[:500]
        logger.error("calle %s exited with code %s: %s", tool, proc.returncode, detail)
        raise CalleError(
            f"calle {tool} exited with code {proc.returncode}: {detail}",
            returncode=proc.returncode,
            stderr=proc.stderr,
        )

    try:
        payload = json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        logger.error(
            "calle %s returned non-JSON output: %s", tool, proc.stdout.strip()[:500]
        )
        raise CalleError(
            f"calle {tool} returned non-JSON output: {proc.stdout.strip()[:500]}"
        ) from exc

    _check_payload(payload, tool)
    logger.info("calle %s completed successfully", tool)
    return payload


def structured_result(tool_payload: object) -> dict:
    if not isinstance(tool_payload, dict):
        return {}
    result = tool_payload.get("result")
    if isinstance(result, dict):
        sc = result.get("structuredContent")
        if isinstance(sc, dict):
            return sc
        return result
    return tool_payload


def plan_call(
    user_input: str,
    plan_id: str | None = None,
    language: str | None = None,
) -> dict:
    args: dict = {"user_input": user_input}
    if plan_id:
        args["plan_id"] = plan_id
    if language:
        args["language"] = language
    return _invoke_tool("plan_call", args, DEFAULT_TIMEOUTS["plan_call"])


def run_call(plan_id: str, confirm_token: str) -> dict:
    return _invoke_tool(
        "run_call",
        {"plan_id": plan_id, "confirm_token": confirm_token},
        DEFAULT_TIMEOUTS["run_call"],
    )


def get_call_status(run_id: str) -> dict:
    return _invoke_tool(
        "get_call_run", {"run_id": run_id}, DEFAULT_TIMEOUTS["get_call_run"]
    )
