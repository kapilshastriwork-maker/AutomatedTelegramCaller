import re


def mask_phone(phone: str) -> str:
    phone = phone.strip()
    if len(phone) <= 6:
        return "*" * len(phone)
    return f"{phone[:3]}******{phone[-3:]}"


def mask_phones_in(text: str) -> str:
    def _repl(match):
        return mask_phone(re.sub(r"\s+", "", match.group()))

    return re.sub(r"\+\d(?:[\d\s]{4,20}\d)?", _repl, text)
