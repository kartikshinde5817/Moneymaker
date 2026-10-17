"""Country policy and suppression.

Cold B2B email is lawful in some countries and not others. Rather than leave
that to whoever writes the next feature, the rule lives in one table here and
is stamped onto every company row as `email_policy` at creation time. The
dispatcher refuses anything that is not `allowed`, so there is no code path
that can email a blocked jurisdiction.

Sources for each ruling are in ARCHITECTURE.md section 0.5. Short version:

  GB  PECR reg.22 exempts corporate subscribers -> lawful with opt-out
  US  CAN-SPAM is opt-out based              -> lawful with postal address
  AE  PDPL, less settled for B2B             -> allowed but flagged
  DE  UWG s.7 needs prior consent, incl. B2B -> manual outreach only
  AT  equivalent to DE                       -> manual outreach only
  RU  38-FZ consent + 242-FZ localisation    -> blocked entirely

This is an engineering control, not legal advice.
"""

from __future__ import annotations

import re

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Suppression

ALLOWED = "allowed"
MANUAL_ONLY = "manual_only"
BLOCKED = "blocked"

COUNTRY_POLICY: dict[str, tuple[str, str]] = {
    # --- cold B2B email is lawful, with identification and opt-out ---------
    "GB": (ALLOWED, "PECR reg.22 corporate subscriber exemption; opt-out required"),
    "US": (ALLOWED, "CAN-SPAM opt-out regime; postal address and opt-out required"),
    "IE": (ALLOWED, "ePrivacy Regs B2B permitted with opt-out"),
    "NL": (ALLOWED, "Telecommunicatiewet permits B2B with opt-out"),
    "AE": (ALLOWED, "UAE PDPL - permitted for corporate addresses; review locally"),
    "AU": (ALLOWED, "Spam Act 2003 - inferred consent for a published business "
                    "address relevant to that role; sender ID and opt-out required"),
    "NZ": (ALLOWED, "Unsolicited Electronic Messages Act - inferred consent for "
                    "published business addresses; opt-out required"),
    "SG": (ALLOWED, "Spam Control Act - opt-out regime with required labelling"),
    "IN": (ALLOWED, "No email-specific restriction on B2B; TRAI/DLT rules cover "
                    "SMS and calls, not email"),
    "ZA": (ALLOWED, "ECTA s.45 opt-out regime for electronic marketing"),

    # --- permitted but narrower; worth local advice before scaling --------
    "FR": (ALLOWED, "CNIL permits B2B email related to the recipient's profession, "
                    "with opt-out"),
    "SE": (ALLOWED, "Marketing Act permits B2B with opt-out"),
    "DK": (MANUAL_ONLY, "Marketing Practices Act s.10 requires prior consent"),
    "ES": (MANUAL_ONLY, "LSSI art.21 requires consent or a prior relationship"),
    "IT": (MANUAL_ONLY, "Garante applies consent requirements to B2B"),
    "CA": (MANUAL_ONLY, "CASL requires consent (express or implied) before sending"),

    # --- do not email -----------------------------------------------------
    "DE": (MANUAL_ONLY, "UWG s.7 requires prior consent, including B2B"),
    "AT": (MANUAL_ONLY, "TKG s.174 requires prior consent, including B2B"),
    "RU": (BLOCKED, "38-FZ consent requirement and 242-FZ data localisation"),
}

DEFAULT_POLICY = (MANUAL_ONLY, "No reviewed policy for this country yet")

# Countries the discovery layer targets unless a campaign says otherwise.
DEFAULT_TARGET_COUNTRIES = ["GB", "US", "AU", "IE", "AE", "SG"]

ROLE_PREFIXES = {
    "info", "contact", "hello", "sales", "enquiry", "enquiries", "inquiries",
    "admin", "office", "mail", "team", "support", "bookings", "reservations",
}

# Addresses that are never a prospect: our own tooling, abuse handling, or
# automated senders. Emailing these does nothing but generate complaints.
NEVER_EMAIL_PREFIXES = {
    "abuse", "postmaster", "noreply", "no-reply", "donotreply", "do-not-reply",
    "privacy", "dpo", "legal", "unsubscribe", "webmaster", "hostmaster",
    "security", "spam",
}

DISPOSABLE_DOMAINS = {
    "mailinator.com", "guerrillamail.com", "10minutemail.com", "tempmail.com",
    "throwaway.email", "yopmail.com", "trashmail.com", "getnada.com",
    "sharklasers.com", "dispostable.com",
}


def policy_for_country(country: str | None) -> tuple[str, str]:
    if not country:
        return DEFAULT_POLICY
    return COUNTRY_POLICY.get(country.upper(), DEFAULT_POLICY)


def email_policy_for_country(country: str | None) -> str:
    return policy_for_country(country)[0]


def can_email_country(country: str | None) -> bool:
    return email_policy_for_country(country) == ALLOWED


def is_role_address(email: str) -> bool:
    return email.split("@", 1)[0].lower() in ROLE_PREFIXES


def is_never_email(email: str) -> bool:
    local = email.split("@", 1)[0].lower()
    return local in NEVER_EMAIL_PREFIXES


def is_disposable(email: str) -> bool:
    return email.split("@", 1)[-1].lower() in DISPOSABLE_DOMAINS


def is_suppressed(db: Session, email: str) -> str | None:
    """Return the suppression reason, or None. Checked before every send."""
    email = (email or "").strip().lower()
    if not email or "@" not in email:
        return "invalid address"
    domain = email.split("@", 1)[1]
    row = db.execute(
        select(Suppression).where(
            Suppression.scope == "email", Suppression.value == email
        )
    ).scalar_one_or_none()
    if row:
        return row.reason or "on suppression list"
    row = db.execute(
        select(Suppression).where(
            Suppression.scope == "domain", Suppression.value == domain
        )
    ).scalar_one_or_none()
    if row:
        return row.reason or "domain suppressed"
    return None


def suppress(db: Session, value: str, reason: str, scope: str = "email") -> None:
    """Idempotent. Unsubscribes and hard bounces are permanent."""
    value = (value or "").strip().lower()
    if not value:
        return
    exists = db.execute(
        select(Suppression).where(
            Suppression.scope == scope, Suppression.value == value
        )
    ).scalar_one_or_none()
    if exists:
        return
    db.add(Suppression(scope=scope, value=value, reason=reason[:160]))
    db.flush()


# --- text used in every outbound message ----------------------------------

FORBIDDEN_CLAIM_PATTERNS = [
    re.compile(p, re.I)
    for p in (
        r"\byou (?:said|mentioned|stated|told)\b",
        r"\byou(?:'re| are) looking for\b",
        r"\byou requested\b",
        r"\byour request for\b",
        r"\bas you (?:mentioned|noted|said)\b",
        r"\bi (?:saw|read) your (?:post|message|request)\b",
        r"\byou need\b",
    )
]


def find_unsupported_claims(text: str) -> list[str]:
    """Phrases that assert the prospect asked for something.

    Only legitimate when an `explicit_request` signal exists. The personalizer
    output is checked against this before a draft is allowed to be queued.
    """
    return [m.group(0) for p in FORBIDDEN_CLAIM_PATTERNS if (m := p.search(text or ""))]
