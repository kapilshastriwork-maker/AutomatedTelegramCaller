import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parents[1] / ".env")

from app.calle_client import (  # noqa: E402
    TERMINAL_STATUSES,
    CalleError,
    get_call_status,
    is_terminal,
    plan_call,
    redact,
    run_call,
)

TEST_INPUT_TEMPLATE = (
    "Call [MY_TEST_NUMBER] and just say this is a test call, then say goodbye"
)
POLL_INTERVAL_SECONDS = 2
MAX_POLL_SECONDS = 300


def _structured(tool_payload):
    if not isinstance(tool_payload, dict):
        return {}
    result = tool_payload.get("result")
    if isinstance(result, dict):
        sc = result.get("structuredContent")
        if isinstance(sc, dict):
            return sc
        return result
    return tool_payload


def _print_questions(plan):
    questions = plan.get("clarifying_questions") or [
        q.get("question", "") for q in plan.get("questions", []) if isinstance(q, dict)
    ]
    print("\nPlan is NOT ready to run. Clarifying questions:")
    for q in questions:
        print(f" - {q}")


def main() -> int:
    inspect_only = "--inspect" in sys.argv
    assume_yes = "--yes" in sys.argv

    number = os.getenv("TEST_PHONE_NUMBER", "").strip()
    if not number or "[" in number:
        print(
            "ERROR: set TEST_PHONE_NUMBER in .env to a real destination number "
            "(E.164 recommended, e.g. +15551234567) before running."
        )
        return 2

    user_input = TEST_INPUT_TEMPLATE.replace("[MY_TEST_NUMBER]", number)
    print(f"Planning call with input: {user_input!r}")

    plan = _structured(plan_call(user_input))
    print("[plan response - confirm_token redacted]")
    print(json.dumps(redact(plan), indent=2))

    if not plan.get("ready_to_run"):
        _print_questions(plan)
        return 0

    plan_id = plan.get("plan_id")
    token = plan.get("confirm_token")
    if inspect_only:
        print(f"\n--inspect set: plan {plan_id} is READY but no call was placed.")
        return 0

    if not plan_id or not token:
        print("\nERROR: plan is ready but plan_id/confirm_token missing.")
        return 1

    if not assume_yes:
        answer = input(
            f"\nAbout to place a REAL phone call to {number}. Continue? [y/N] "
        )
        if answer.strip().lower() != "y":
            print("Aborted before run_call. No call was placed.")
            return 0

    run_payload = run_call(str(plan_id), str(token))
    latest_sc = _structured(run_payload)
    print("[run response]")
    print(json.dumps(redact(latest_sc), indent=2))

    run_id = latest_sc.get("run_id") or run_payload.get("run_id")
    if not run_id:
        print("ERROR: no run_id returned by run_call.")
        return 1

    started = time.monotonic()
    while True:
        status = latest_sc.get("status")
        activity = latest_sc.get("activity") or []

        if is_terminal(status):
            break

        elapsed = time.monotonic() - started
        if elapsed > MAX_POLL_SECONDS:
            print(f"\nPolling exceeded {MAX_POLL_SECONDS}s; giving up.")
            return 1

        print("\nPhone call is in progress! Progress:")
        lines = []
        for item in activity:
            ts = str(item.get("ts", ""))[-8:] if isinstance(item, dict) else ""
            msg = item.get("message", "") if isinstance(item, dict) else str(item)
            lines.append(f"- {ts} {msg}".rstrip())
        print("\n".join(lines) if lines else f"- Status: {status}")

        time.sleep(POLL_INTERVAL_SECONDS)
        latest_sc = _structured(get_call_status(str(run_id)))

    nested = (
        latest_sc.get("result") if isinstance(latest_sc.get("result"), dict) else {}
    )
    summary = (
        nested.get("summary")
        or nested.get("post_summary")
        or latest_sc.get("summary")
        or latest_sc.get("post_summary")
        or "(none)"
    )
    transcript = latest_sc.get("transcript")

    print(f"\n[Status]\n{status}")
    print(f"\n[Call Summary]\n{summary}")
    print(f"[Run id] {run_id}")
    print("\n[Transcript]")
    print(transcript if transcript else "Not available.")

    out_path = Path(__file__).resolve().parents[1] / "test_call_transcript.json"
    out_path.write_text(json.dumps(redact(latest_sc), indent=2), encoding="utf-8")
    print(f"\n[Full redacted final payload written to] {out_path}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except CalleError as exc:
        print(f"CALLE ERROR: {exc}")
        raise SystemExit(1)
