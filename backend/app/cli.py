"""
NEXUS AI · app/cli.py
Account commands run from the server shell. Self-registration always creates
`user` accounts, so the first admin is promoted here instead of over HTTP.

Usage (inside the api container):
    python -m app.cli make-admin <username>
    python -m app.cli set-password <username>    # prompts; the password is never echoed
    python -m app.cli redact-pii [--apply]       # remove personal data from existing tickets
    python -m app.cli sso-setup                  # create/repair the Keycloak realm for SSO
    python -m app.cli sso-user <username> --email <e-mail> --role admin|agent|analyst|user
"""

import argparse
import asyncio
import getpass
import sys

from sqlalchemy import select


async def make_admin(username: str) -> int:
    from app.database import engine, get_db_context
    from app.models.audit_log import AuditLog
    from app.models.user import User, UserRole

    try:
        async with get_db_context() as db:
            result = await db.execute(select(User).where(User.username == username))
            user = result.scalar_one_or_none()
            if user is None:
                print(f"No user named '{username}'. Register it first via POST /api/v1/auth/register.")
                return 1
            if user.role == UserRole.ADMIN:
                print(f"'{username}' is already an admin.")
                return 0

            old_role = user.role
            user.role = UserRole.ADMIN
            db.add(AuditLog(
                user_id=None,
                action="user.role_change",
                resource_type="user",
                resource_id=str(user.id),
                details={"old_role": old_role.value, "new_role": "admin", "via": "cli"},
            ))
        print(f"'{username}' promoted from {old_role.value} to admin.")
        return 0
    finally:
        await engine.dispose()


MIN_PASSWORD_LENGTH = 8   # same rule as registration


async def set_password(username: str, password: str) -> int:
    from app.core.security import hash_password
    from app.database import engine, get_db_context
    from app.models.audit_log import AuditLog
    from app.models.user import User

    try:
        async with get_db_context() as db:
            result = await db.execute(select(User).where(User.username == username))
            user = result.scalar_one_or_none()
            if user is None:
                print(f"No user named '{username}'.")
                return 1
            if user.is_keycloak_user:
                print(f"'{username}' signs in through Keycloak; change the password there.")
                return 1

            user.hashed_password = hash_password(password)
            db.add(AuditLog(
                user_id=None,
                action="user.password_change",
                resource_type="user",
                resource_id=str(user.id),
                details={"via": "cli"},
            ))
        print(f"Password changed for '{username}'.")
        return 0
    finally:
        await engine.dispose()


async def redact_existing_tickets(apply: bool) -> int:
    """
    Run PII redaction over tickets saved before it was switched on.
    Without --apply it only reports what it would change.
    """
    from collections import Counter

    from app.database import engine, get_db_context
    from app.middleware.security import redact_fields, warm_up_pii
    from app.models.audit_log import AuditLog
    from app.models.ticket import Ticket

    warm_up_pii()
    changed, types = 0, Counter()
    try:
        async with get_db_context() as db:
            tickets = (await db.execute(select(Ticket).order_by(Ticket.created_at))).scalars().all()
            for t in tickets:
                clean, found = redact_fields(
                    title=t.title, description=t.description, resolution_notes=t.resolution_notes,
                )
                if not found:
                    continue
                changed += 1
                types.update(found)
                print(f"  {t.ticket_number}: {', '.join(found)}")
                if apply:
                    t.title = (clean["title"] or "")[:500]
                    t.description = clean["description"]
                    t.resolution_notes = clean["resolution_notes"]
                    db.add(AuditLog(
                        user_id=None, action="ticket.pii_redacted", resource_type="ticket",
                        resource_id=str(t.id), details={"pii_redacted": found, "via": "cli"},
                    ))
            if not apply:
                await db.rollback()
        summary = ", ".join(f"{k} x{v}" for k, v in sorted(types.items())) or "none"
        verb = "Redacted" if apply else "Would redact"
        print(f"{verb} {changed} of {len(tickets)} tickets. Types found: {summary}.")
        if changed and not apply:
            print("Run again with --apply to save the changes.")
        return 0
    finally:
        await engine.dispose()


def sso_setup(app_url: str | None) -> int:
    from app.config import settings
    from app.core.sso import KeycloakAdmin, ensure_realm, public_url

    try:
        admin = KeycloakAdmin()
        for line in ensure_realm(admin, app_url):
            print(f"  {line}")
    except Exception as e:
        print(f"Keycloak setup failed: {e}")
        return 1
    print(f"Realm '{settings.keycloak_realm}' is ready at {public_url()}/realms/{settings.keycloak_realm}")
    print("Next: add a user with  python -m app.cli sso-user <username> --email <e-mail> --role admin")
    return 0


def sso_user(username: str, email: str, role: str, full_name: str | None, keep_password: bool) -> int:
    from app.core.sso import KeycloakAdmin, upsert_user
    from app.models.user import UserRole

    password = None
    if not keep_password:
        password = prompt_new_password()
        if password is None:
            return 1
    try:
        admin = KeycloakAdmin()
        outcome = upsert_user(admin, username, email, UserRole(role), password, full_name)
    except Exception as e:
        print(f"Could not save the Keycloak user: {e}")
        return 1
    print(f"Keycloak user '{username}' {outcome} with role {role}. Sign in with \"Sign in with Keycloak\".")
    return 0


def prompt_new_password() -> str | None:
    """Ask twice without echoing; None if the two entries differ or the password is too short."""
    first = getpass.getpass("New password: ")
    if len(first) < MIN_PASSWORD_LENGTH:
        print(f"Password must be at least {MIN_PASSWORD_LENGTH} characters.")
        return None
    if getpass.getpass("Repeat new password: ") != first:
        print("The passwords do not match.")
        return None
    return first


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m app.cli")
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("make-admin", help="Give an existing user the admin role")
    p.add_argument("username")
    p = sub.add_parser("set-password", help="Set a local user's password (prompts for it)")
    p.add_argument("username")
    p = sub.add_parser("sso-setup", help="Create or repair the Keycloak realm, roles and web-app client")
    p.add_argument("--app-url", help="Address of the NEXUS web app (default: PUBLIC_APP_URL)")
    p = sub.add_parser("sso-user", help="Create or update a Keycloak user (prompts for the password)")
    p.add_argument("username")
    p.add_argument("--email", required=True)
    p.add_argument("--role", choices=["admin", "agent", "analyst", "user"], default="user")
    p.add_argument("--name", dest="full_name", help="Full name")
    p.add_argument("--keep-password", action="store_true", help="Update e-mail/role only")
    p = sub.add_parser("redact-pii", help="Remove personal data from existing tickets (dry run unless --apply)")
    p.add_argument("--apply", action="store_true", help="Save the changes")
    args = parser.parse_args(argv)

    if args.command == "make-admin":
        return asyncio.run(make_admin(args.username))
    if args.command == "set-password":
        password = prompt_new_password()
        if password is None:
            return 1
        return asyncio.run(set_password(args.username, password))
    if args.command == "sso-setup":
        return sso_setup(args.app_url)
    if args.command == "sso-user":
        return sso_user(args.username, args.email, args.role, args.full_name, args.keep_password)
    if args.command == "redact-pii":
        return asyncio.run(redact_existing_tickets(args.apply))
    return 2


if __name__ == "__main__":
    sys.exit(main())
