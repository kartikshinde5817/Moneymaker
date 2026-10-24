#!/usr/bin/env python3
"""Start Money Maker.

    python3 run.py

No virtualenv activation needed. If the dependencies are not importable this
re-launches itself inside ./venv, creating it and installing requirements the
first time. Flags:

    --no-browser   don't open the dashboard
    --reinstall    force dependency reinstall
    --check        print configuration status and exit
    --send-only    send approved emails from this computer, nothing else
                   (for servers that block SMTP, like Render's free plan)
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parent
VENV = BASE / "venv"
# Guard against re-exec looping if the venv python still cannot import things.
GUARD = "MONEYMAKER_BOOTSTRAPPED"

if sys.version_info < (3, 11):
    sys.exit(f"Python 3.11+ required (this is {sys.version.split()[0]}).")


def venv_python() -> Path:
    if os.name == "nt":
        return VENV / "Scripts" / "python.exe"
    return VENV / "bin" / "python"


def deps_ready() -> bool:
    try:
        import fastapi, sqlalchemy, httpx, apscheduler  # noqa: F401
        return True
    except ImportError:
        return False


def venv_deps_ready(python: Path) -> bool:
    """Are the dependencies importable inside the venv (not this interpreter)?"""
    code = "import fastapi, sqlalchemy, httpx, apscheduler"
    return subprocess.run([str(python), "-c", code], capture_output=True).returncode == 0


def install(python: Path) -> None:
    """Install requirements. Uses `python -m pip`, never the pip script.

    A venv moved or renamed after creation keeps the old absolute path in its
    pip shebang and fails with "bad interpreter"; calling pip as a module
    sidesteps that entirely.
    """
    print("Installing dependencies (first run takes a minute)…")
    result = subprocess.run(
        [str(python), "-m", "pip", "install", "-q", "--timeout", "60", "--retries", "10",
         "-r", str(BASE / "requirements.txt")],
    )
    if result.returncode != 0:
        sys.exit("Dependency install failed. Run it by hand:\n"
                 f"  {python} -m pip install -r requirements.txt")


def bootstrap() -> None:
    """Re-launch inside the venv, creating it if necessary."""
    python = venv_python()
    if not python.exists():
        print(f"Creating virtualenv in {VENV}…")
        subprocess.run([sys.executable, "-m", "venv", str(VENV)], check=True)
        python = venv_python()
        install(python)
    elif "--reinstall" in sys.argv or not venv_deps_ready(python):
        install(python)

    env = dict(os.environ, **{GUARD: "1"})
    args = [a for a in sys.argv[1:] if a != "--reinstall"]
    os.execve(str(python), [str(python), str(BASE / "run.py"), *args], env)


# Re-exec into the venv before importing anything from app/.
if not deps_ready():
    if os.environ.get(GUARD):
        sys.exit("Dependencies are still missing inside the virtualenv.\n"
                 f"Try:  {venv_python()} -m pip install -r requirements.txt")
    bootstrap()


from app.config import settings  # noqa: E402


def _public_ok() -> bool:
    from app.services.mailer import public_url_is_reachable
    return public_url_is_reachable()


def status() -> list[tuple[str, bool, str]]:
    """What is configured, and what each gap costs."""
    return [
        ("SMTP sending", bool(settings.smtp_host and settings.smtp_user),
         "set SMTP_* in .env"),
        ("Postal address", bool(settings.company_postal_address),
         "COMPANY_POSTAL_ADDRESS — required by US law, blocks all sending"),
        ("Sender name", bool(settings.sender_name), "SENDER_NAME"),
        ("Reply detection", settings.imap_ready, "set IMAP_* in .env"),
        ("Telegram alerts", settings.telegram_ready,
         "run: python3 tools/telegram_setup.py <BOT_TOKEN>"),
        ("Web search leads", bool(settings.serper_api_key),
         "SERPER_API_KEY — biggest single lead-volume win, free at serper.dev"),
        ("AI-written emails", bool(settings.anthropic_api_key),
         "ANTHROPIC_API_KEY — otherwise templates are used"),
        ("Public URL", _public_ok(),
         "PUBLIC_BASE_URL — unsubscribe links would be dead, blocks all sending"),
    ]


def print_status() -> bool:
    print("\n  Configuration")
    blocking = False
    for name, ok, hint in status():
        mark = "OK  " if ok else "--  "
        print(f"   {mark}{name:<18}{'' if ok else hint}")
        if not ok and "blocks all sending" in hint:
            blocking = True
    return blocking


def verify_mail() -> None:
    """Actually log in. Having SMTP_* set is not the same as them working -
    a wrong mailbox name looks fine in the config and fails only at send."""
    import imaplib
    import smtplib
    import ssl

    if not settings.smtp_host:
        return
    print("\n  Mailbox check")
    ctx = ssl.create_default_context()
    try:
        if settings.smtp_use_ssl or settings.smtp_port == 465:
            conn = smtplib.SMTP_SSL(settings.smtp_host, settings.smtp_port,
                                    timeout=20, context=ctx)
        else:
            conn = smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=20)
            conn.ehlo()
            conn.starttls(context=ctx)
        with conn:
            conn.login(settings.smtp_user, settings.smtp_pass)
        print(f"   OK  SMTP login as {settings.smtp_user}")
    except Exception as exc:
        print(f"   --  SMTP login FAILED as {settings.smtp_user}: "
              f"{type(exc).__name__}")
        print("       Check SMTP_USER is a mailbox that exists, and the password.")

    if settings.imap_ready:
        try:
            with imaplib.IMAP4_SSL(settings.imap_host, settings.imap_port) as box:
                box.login(settings.imap_user or settings.smtp_user,
                          settings.imap_pass or settings.smtp_pass)
            print(f"   OK  IMAP login as {settings.imap_user or settings.smtp_user}")
        except Exception as exc:
            print(f"   --  IMAP login FAILED: {type(exc).__name__}")


SEND_EVERY_SECONDS = 60


def send_only() -> None:
    """Send the approved-email queue from this computer, and do nothing else.

    No dashboard, no scheduler, no campaigns: the server keeps doing those.
    This only needs DATABASE_URL (the same database the server uses), the
    SMTP_* mailbox, and PUBLIC_BASE_URL set to the server's address so the
    unsubscribe links in each email work.
    """
    import time

    from app.db import session_scope
    from app.services import mailer, pipeline, sender

    print("\n" + "=" * 66)
    print("  Money Maker — email sender (this computer)")
    db_label = settings.database_url.split("@")[-1].split("?")[0]
    print(f"  Database:   {db_label}")
    print(f"  Mailbox:    {settings.smtp_user or '(not set)'} via {settings.smtp_host or '(not set)'}")
    print("=" * 66)

    problems = []
    if settings.database_url.startswith("sqlite"):
        problems.append("DATABASE_URL points at the local file database. Put the same "
                        "postgresql+psycopg://… URL you use on Render into .env.")
    if not mailer.provider_ready():
        problems.append("SMTP is not set. Add SMTP_HOST, SMTP_PORT, SMTP_USER and SMTP_PASS to .env.")
    if not _public_ok():
        problems.append("PUBLIC_BASE_URL must be your Render address (https://….onrender.com), "
                        "or the unsubscribe links in the emails will not work.")
    if problems:
        print("\n  Cannot start:")
        for p in problems:
            print(f"   - {p}")
        sys.exit(1)

    verify_mail()
    print(f"\n  Checking for approved emails every {SEND_EVERY_SECONDS} s. "
          "Leave this window open; press Ctrl+C to stop.\n")
    while True:
        stamp = time.strftime("%H:%M:%S")
        try:
            with session_scope() as db:
                result = pipeline.dispatch_queue(db, limit=20)
                sender.heartbeat(db, sent=result["sent"])
            if result["sent"] or result["failed"] or result["skipped"]:
                print(f"  {stamp}  sent {result['sent']}, failed {result['failed']}, "
                      f"skipped {result['skipped']}")
                for line in result["details"][:10]:
                    print(f"            {line}")
            else:
                print(f"  {stamp}  nothing waiting", end="\r", flush=True)
        except KeyboardInterrupt:
            raise
        except Exception as exc:  # noqa: BLE001 - a network blip must not stop the sender
            print(f"  {stamp}  error: {type(exc).__name__}: {str(exc)[:200]} (will retry)")
        time.sleep(SEND_EVERY_SECONDS)


def main() -> None:
    if "--send-only" in sys.argv:
        try:
            send_only()
        except KeyboardInterrupt:
            print("\n  Sender stopped.")
        return

    url = f"http://{settings.host}:{settings.port}"
    print("\n" + "=" * 66)
    print("  Money Maker — lead generation & outreach")
    print(f"  Dashboard:  {url}")
    print(f"  Database:   {settings.database_url.split('///')[-1]}")
    blocking = print_status()
    if blocking:
        print("\n  Drafts will queue but nothing can be sent yet.")
    print("=" * 66 + "\n")

    if "--check" in sys.argv:
        verify_mail()
        return

    import uvicorn

    if "--no-browser" not in sys.argv:
        import threading
        import webbrowser
        threading.Timer(1.5, lambda: webbrowser.open(url)).start()

    uvicorn.run("app.main:app", host=settings.host, port=settings.port,
                log_level="info")


if __name__ == "__main__":
    main()
