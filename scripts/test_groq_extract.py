import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parents[1] / ".env")

from app.groq_client import GroqError, extract_call_details, is_configured  # noqa: E402

CASE_A = (
    "Book a dental cleaning at Dr Sharma Dental Clinic, +919876543210, "
    "next Tuesday at 10am."
)
CASE_B = "Please book an eye check-up at Vision Care next Friday morning."
CASE_C = (
    "Call Dr Sharma Dental at +919876543210 at 4:45pm today, to book a "
    "cleaning for next Tuesday at 10am."
)
CASE_D = "Call the dentist at +-17610651145 tomorrow 9am about a root canal."


def show(label: str, details: dict) -> None:
    print(f"--- {label} ---")
    for field in (
        "clinic_or_doctor",
        "phone",
        "reason",
        "preferred_date",
        "preferred_time",
        "call_at",
    ):
        print(f"{field}: {details.get(field)}")
    print(f"missing_fields: {details.get('missing_fields')}")
    print(f"phone_invalid: {details.get('phone_invalid')}")


def main() -> int:
    if not is_configured():
        print("GROQ_API_KEY not set — cannot run live extraction tests.")
        return 2

    failures = []

    print("(a) complete message")
    a = extract_call_details(CASE_A)
    show("case A", a)
    if a["missing_fields"]:
        failures.append(f"(a) expected no missing fields, got {a['missing_fields']}")
    if not (a["phone"] and a["clinic_or_doctor"]):
        failures.append("(a) expected clinic and phone to be extracted")

    print("\n(b) message missing the phone number")
    b = extract_call_details(CASE_B)
    show("case B", b)
    if "phone" not in b["missing_fields"]:
        failures.append(
            f"(b) expected 'phone' in missing_fields, got {b['missing_fields']}"
        )
    if b["phone"] is not None:
        failures.append("(b) model invented a phone number")

    print("\n(c) Groq failure simulation (invalid API key)")
    os.environ["GROQ_API_KEY"] = "gsk_invalid_key_for_fallback_test"
    try:
        extract_call_details(CASE_A)
        failures.append("(c) expected GroqError with invalid key")
    except GroqError as exc:
        print(f"[fallback] GroqError raised as designed: {str(exc)[:80]}...")
        print("[fallback] bot would switch to the guided WHO->WHAT flow")
    os.environ.pop("GROQ_API_KEY", None)
    load_dotenv(".env", override=True)

    print("\n(d) two-time message: call_at vs appointment slot (schedule mode)")
    d = extract_call_details(CASE_C, include_call_at=True)
    show("case D", d)
    if not d.get("call_at"):
        failures.append("(d) call_at missing from two-time message")
    if not d.get("preferred_date") and not d.get("preferred_time"):
        failures.append("(d) appointment slot lost")
    if d.get("missing_fields"):
        failures.append(f"(d) expected no missing fields, got {d['missing_fields']}")

    print("\n(e) malformed phone number flagged, not invented")
    e = extract_call_details(CASE_D, include_call_at=True)
    show("case E", e)
    if "phone" not in (e.get("missing_fields") or []):
        failures.append("(e) malformed phone should land in missing_fields")
    if not e.get("phone_invalid"):
        failures.append("(e) phone_invalid flag not set for +-17610651145")

    print()
    if failures:
        for failure in failures:
            print(f"FAIL: {failure}")
        return 1
    print("ALL GROQ EXTRACTION TESTS PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
