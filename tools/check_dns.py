#!/usr/bin/env python3
"""Check the sending domain's email authentication.

    venv/bin/python tools/check_dns.py [domain]

SPF, DKIM and DMARC are what mailbox providers use to decide whether your mail
is really from you. Missing DKIM in particular pushes cold mail to spam, and
Gmail and Yahoo require all three from bulk senders.
"""
from __future__ import annotations

import sys

import dns.resolver

# Hostinger publishes DKIM on these selectors; the rest are common elsewhere.
SELECTORS = ("hostingermail1", "hostingermail2", "hostingermail3",
             "default", "google", "k1", "s1", "selector1", "dkim", "mail")


def lookup(name: str, rr: str = "TXT") -> list[str]:
    try:
        return [str(a).strip('"').replace('" "', "") for a in
                dns.resolver.resolve(name, rr, lifetime=10)]
    except Exception:
        return []


def main() -> int:
    domain = (sys.argv[1] if len(sys.argv) > 1 else "").strip()
    if not domain:
        from app.config import settings
        domain = (settings.sender_email or "@").split("@")[-1]
    if not domain:
        print("Pass a domain, or set SENDER_EMAIL in .env")
        return 1

    print(f"Checking {domain}\n")
    ok = True

    mx = lookup(domain, "MX")
    print(f"  MX     {'OK  ' if mx else 'NONE'}  {mx[0] if mx else 'no mail server'}")

    spf = next((t for t in lookup(domain) if t.lower().startswith("v=spf1")), "")
    print(f"  SPF    {'OK  ' if spf else 'MISS'}  {spf or 'not published'}")
    ok &= bool(spf)

    found = ""
    for sel in SELECTORS:
        for rr in ("CNAME", "TXT"):
            hit = lookup(f"{sel}._domainkey.{domain}", rr)
            if hit:
                found = f"{sel} -> {hit[0][:58]}"
                break
        if found:
            break
    print(f"  DKIM   {'OK  ' if found else 'MISS'}  {found or 'not found on common selectors'}")
    ok &= bool(found)

    dmarc = next((t for t in lookup(f"_dmarc.{domain}") if "DMARC1" in t), "")
    print(f"  DMARC  {'OK  ' if dmarc else 'MISS'}  {dmarc or 'not published'}")
    ok &= bool(dmarc)

    print()
    if not found:
        print("  DKIM is missing. In Hostinger: hPanel > Emails > your domain >")
        print("  DKIM, then enable it. Without DKIM your cold mail lands in spam")
        print("  far more often, whatever the sending limit allows.")
    if dmarc and "p=none" in dmarc:
        print("  DMARC is on p=none (monitor only). Fine while warming up; move")
        print("  to p=quarantine once DKIM is in place and mail is landing.")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
