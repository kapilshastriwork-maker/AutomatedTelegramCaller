import json
import re

import httpx

from app.config import get_groq_key, get_groq_model

GROQ_API_URL = "https://api.groq.com/openai/v1/chat/completions"
REQUEST_TIMEOUT = 15
MAX_TOKENS = 2000

_JSON_RETRY_ATTEMPTS = 2

DETAIL_FIELDS = (
    "clinic_or_doctor",
    "phone",
    "reason",
    "preferred_date",
    "preferred_time",
    "patient_name",
)
SCHED_FIELDS = DETAIL_FIELDS + ("call_at",)

_PHONE_RE = re.compile(r"\+[1-9]\d{6,14}")

_SYSTEM_PROMPT = (
    "You extract doctor/clinic appointment call details from a user message. "
    "There are two different times to distinguish: "
    "call_at is the exact date/time when the phone call itself should be "
    "PLACED (e.g. 'today at 4:45pm', 'tomorrow 9am'); "
    "preferred_date and preferred_time describe the appointment slot the "
    "user wants to REQUEST from the clinic (e.g. 'next Tuesday', '10am'). "
    "Also extract the patient's name if the user provides it (e.g. 'for John' or 'patient: Jane'). "
    "Return ONLY a JSON object with exactly these keys: "
    "clinic_or_doctor, phone, reason, preferred_date, preferred_time, "
    "call_at, patient_name. "
    "Use null for any field not explicitly present in the message. "
    "Never invent or guess a phone number, date, or time - if the user wrote "
    "one, copy it exactly as written; otherwise use null. "
    "Keep phone exactly as written by the user including country code."
)


class GroqError(RuntimeError):
    pass


class _JsonValidateRetry(Exception):
    pass


def is_configured() -> bool:
    try:
        return bool(get_groq_key())
    except RuntimeError:
        return False


def _clean(value) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    lowered = text.lower()
    if not text or lowered in {"null", "none", "n/a"}:
        return None
    return text


def _clean_phone(value) -> str | None:
    raw = _clean(value)
    if raw is None:
        return None
    compact = re.sub(r"\s+", "", raw)
    if not _PHONE_RE.fullmatch(compact):
        return None
    return compact


def _groq_chat(system_prompt: str, text: str) -> dict:
    if not text.strip():
        raise GroqError("empty message")

    headers = {"Authorization": f"Bearer {get_groq_key()}"}
    body = {
        "model": get_groq_model(),
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": text},
        ],
        "temperature": 0,
        "response_format": {"type": "json_object"},
        "max_tokens": MAX_TOKENS,
    }

    payload = None
    retry_exc: Exception | None = None
    for _ in range(_JSON_RETRY_ATTEMPTS):
        try:
            response = httpx.post(
                GROQ_API_URL, headers=headers, json=body, timeout=REQUEST_TIMEOUT
            )
            if response.status_code == 400 and "json_validate_failed" in response.text:
                raise _JsonValidateRetry(response.text[:200])
            response.raise_for_status()
            payload = response.json()
            break
        except _JsonValidateRetry as exc:
            retry_exc = exc
        except Exception as exc:
            raise GroqError(f"Groq request failed: {exc}") from exc
    else:
        raise GroqError(
            f"Groq JSON-mode generation failed after {_JSON_RETRY_ATTEMPTS} "
            f"attempts: {retry_exc}"
        )

    try:
        content = payload["choices"][0]["message"]["content"]
        data = json.loads(content)
        if not isinstance(data, dict):
            raise ValueError("response JSON is not an object")
    except Exception as exc:
        raise GroqError(f"Groq returned unparseable content: {exc}") from exc
    return data


def extract_call_details(text: str, include_call_at: bool = False) -> dict:
    data = _groq_chat(_SYSTEM_PROMPT, text)

    fields = SCHED_FIELDS if include_call_at else DETAIL_FIELDS
    details: dict[str, str | None | list[str] | bool] = {
        field: (
            _clean_phone(data.get(field))
            if field == "phone"
            else _clean(data.get(field))
        )
        for field in fields
    }
    phone_invalid = bool(_clean(data.get("phone"))) and not details.get("phone")
    missing = [field for field in fields if not details[field]]
    details["missing_fields"] = missing
    details["phone_invalid"] = phone_invalid
    return details


_AROUND_SYSTEM_PROMPT = (
    "You parse a multi-clinic appointment request. The user lists several "
    "clinics, each with a phone number, plus ONE shared appointment request "
    "that will be attempted at every clinic. Return ONLY a JSON object with "
    "exactly these keys: reason, preferred_date, preferred_time, targets. "
    "targets must be an array with one object per clinic the user listed, in "
    "the order given, each object having exactly the keys name and phone. "
    "Use null for any field not explicitly present. Never invent or guess a "
    "phone number, date, or time - copy exactly as written; if a clinic has "
    "no readable number use null for its phone."
)


_EARLIEST_SYSTEM_PROMPT = (
    "You parse a request to check the earliest available appointment across "
    "multiple clinics. The user lists several clinics, each with a phone "
    "number, plus ONE shared request describing what they want to book. "
    "Return ONLY a JSON object with exactly these keys: "
    "reason, preferred_date, preferred_time, targets. "
    "targets must be an array with one object per clinic the user listed, in "
    "the order given, each object having exactly the keys name and phone. "
    "Use null for any field not explicitly present. Never invent or guess a "
    "phone number, date, or time - copy exactly as written; if a clinic has "
    "no readable number use null for its phone."
)


_ALTERNATIVES_SYSTEM_PROMPT = (
    "Given a transcript or summary from a clinic phone call, extract any "
    "alternative appointment dates/times the clinic offered. "
    "Return ONLY a JSON object with exactly one key: alternatives. "
    "The value of alternatives is an array of objects, each with keys: "
    "raw_phrase (the exact words the clinic used, e.g. 'Tuesday 3pm' or 'Wednesday morning'), "
    "date (best-effort YYYY-MM-DD or null if the date cannot be determined), "
    "time (best-effort HH:MM 24h or null if the time cannot be determined). "
    "If no alternatives were offered, return {\"alternatives\": []}. "
    "Never invent dates or times - copy from the text exactly or use null. "
    "Ignore any other information in the text."
)


def extract_booking_alternatives(text: str) -> list[dict]:
    data = _groq_chat(_ALTERNATIVES_SYSTEM_PROMPT, text)
    alternatives = data.get("alternatives")
    if not isinstance(alternatives, list):
        return []
    result = []
    for alt in alternatives:
        if not isinstance(alt, dict):
            continue
        raw_phrase = _clean(alt.get("raw_phrase"))
        date_val = _clean(alt.get("date"))
        time_val = _clean(alt.get("time"))
        result.append(
            {
                "raw_phrase": raw_phrase,
                "date": date_val,
                "time": time_val,
            }
        )
    return result


def extract_call_around(text: str) -> dict:
    data = _groq_chat(_AROUND_SYSTEM_PROMPT, text)

    targets_raw = data.get("targets")
    targets: list[dict] = []
    if isinstance(targets_raw, list):
        for item in targets_raw:
            if not isinstance(item, dict):
                continue
            name = _clean(item.get("name"))
            raw_phone = _clean(item.get("phone"))
            phone = _clean_phone(raw_phone)
            if name is None and phone is None and raw_phone is None:
                continue
            targets.append(
                {
                    "name": name,
                    "phone": phone,
                    "invalid": raw_phone is not None and phone is None,
                }
            )

    valid_targets = [t for t in targets if t["phone"]]
    invalid_names = [
        (t["name"] or t.get("phone") or "unnamed clinic")
        for t in targets
        if not t["phone"]
    ]
    reason = _clean(data.get("reason"))
    missing = []
    if reason is None:
        missing.append("reason")
    if len(valid_targets) < 1:
        missing.append("targets")

    return {
        "reason": reason,
        "preferred_date": _clean(data.get("preferred_date")),
        "preferred_time": _clean(data.get("preferred_time")),
        "targets": targets,
        "valid_count": len(valid_targets),
        "invalid_names": invalid_names,
        "missing_fields": missing,
    }


def extract_earliest_details(text: str) -> dict:
    data = _groq_chat(_EARLIEST_SYSTEM_PROMPT, text)

    targets_raw = data.get("targets")
    targets: list[dict] = []
    if isinstance(targets_raw, list):
        for item in targets_raw:
            if not isinstance(item, dict):
                continue
            name = _clean(item.get("name"))
            raw_phone = _clean(item.get("phone"))
            phone = _clean_phone(raw_phone)
            if name is None and phone is None and raw_phone is None:
                continue
            targets.append(
                {
                    "name": name,
                    "phone": phone,
                    "invalid": raw_phone is not None and phone is None,
                }
            )

    valid_targets = [t for t in targets if t["phone"]]
    invalid_names = [
        (t["name"] or t.get("phone") or "unnamed clinic")
        for t in targets
        if not t["phone"]
    ]
    reason = _clean(data.get("reason"))
    missing = []
    if reason is None:
        missing.append("reason")
    if len(valid_targets) < 1:
        missing.append("targets")

    return {
        "reason": reason,
        "preferred_date": _clean(data.get("preferred_date")),
        "preferred_time": _clean(data.get("preferred_time")),
        "targets": targets,
        "valid_count": len(valid_targets),
        "invalid_names": invalid_names,
        "missing_fields": missing,
    }


_CHAIN_SYSTEM_PROMPT = (
    "You parse a sequential fallback call request. The user lists an ordered "
    "sequence of clinics (or people) to call as fallbacks: typically phrased "
    "as 'call X; if no slot / if not available / if X doesn't work, call Y; "
    "if neither works, tell me'. The first clinic is the primary attempt; "
    "subsequent clinics are tried only if the earlier one cannot book the "
    "requested slot.\n\n"
    "Shared fields apply to every step unless a step explicitly overrides "
    "them (e.g. 'if no slot this week, try Vision Care next week').\n\n"
    "Return ONLY a JSON object with exactly these keys: "
    "patient_name, reason, preferred_date, preferred_time, steps. "
    "steps is an array with one object per clinic the user listed, IN THE "
    "ORDER GIVEN, each object having exactly the keys "
    "clinic_or_doctor, phone, reason, preferred_date, preferred_time. "
    "A step's reason/preferred_date/preferred_time override the shared "
    "ones if set; otherwise the shared value applies at execution time.\n\n"
    "Use null for any field not explicitly present. Never invent or guess a "
    "phone number, date, or time - copy exactly as written; if a clinic has "
    "no readable number use null for its phone."
)


def extract_chain(text: str) -> dict:
    data = _groq_chat(_CHAIN_SYSTEM_PROMPT, text)

    steps_raw = data.get("steps")
    steps: list[dict] = []
    if isinstance(steps_raw, list):
        for item in steps_raw:
            if not isinstance(item, dict):
                continue
            name = _clean(item.get("clinic_or_doctor"))
            raw_phone = _clean(item.get("phone"))
            phone = _clean_phone(raw_phone)
            if name is None and phone is None and raw_phone is None:
                continue
            steps.append(
                {
                    "name": name,
                    "phone": phone,
                    "invalid": raw_phone is not None and phone is None,
                    "reason": _clean(item.get("reason")),
                    "preferred_date": _clean(item.get("preferred_date")),
                    "preferred_time": _clean(item.get("preferred_time")),
                }
            )

    valid_steps = [s for s in steps if s["phone"]]
    invalid_names = [
        (s["name"] or s.get("phone") or "unnamed clinic")
        for s in steps
        if not s["phone"]
    ]

    reason = _clean(data.get("reason"))
    missing = []
    if reason is None:
        missing.append("reason")
    if len(valid_steps) < 2:
        missing.append("steps")

    return {
        "patient_name": _clean(data.get("patient_name")),
        "reason": reason,
        "preferred_date": _clean(data.get("preferred_date")),
        "preferred_time": _clean(data.get("preferred_time")),
        "steps": steps,
        "valid_count": len(valid_steps),
        "invalid_names": invalid_names,
        "missing_fields": missing,
    }
