"""Mobile numbers: one normal form, so they can be stored, searched and dialled.

People type "+91 98765 43210", "(415) 555-2671" with a country picked beside it, or
"0044 7700 900123". All of these become E.164: a plus sign, the country code and
the number, digits only, 8 to 15 digits in total.
"""

from __future__ import annotations

import re


class PhoneError(ValueError):
    pass


def normalize(raw: str, default_code: str = "") -> str:
    """Return +<digits>, or raise PhoneError with a message fit to show the user."""
    text = (raw or "").strip()
    if not text:
        raise PhoneError("Please enter your mobile number.")
    if re.search(r"[A-Za-z]", text):
        raise PhoneError("A mobile number can only contain digits, spaces and + - ( ).")
    digits = re.sub(r"\D", "", text)
    if text.startswith("00"):
        digits = digits[2:]
    elif text.startswith("+"):
        pass
    else:
        code = re.sub(r"\D", "", default_code or "")
        if not code:
            raise PhoneError("Please include the country code, for example +91 98765 43210.")
        digits = code + digits.lstrip("0")
    if not 8 <= len(digits) <= 15 or digits.startswith("0"):
        raise PhoneError("That does not look like a valid mobile number. Check the country code and digits.")
    return "+" + digits
