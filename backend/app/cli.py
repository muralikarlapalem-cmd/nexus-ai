"""
NEXUS AI · app/cli.py
Account commands run from the server shell. Self-registration always creates
`user` accounts, so the first admin is promoted here instead of over HTTP.

Usage (inside the api container):
    python -m app.cli make-admin <username>
    python -m app.cli set-password <username>    # prompts; the password is never echoed
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
    args = parser.parse_args(argv)

    if args.command == "make-admin":
        return asyncio.run(make_admin(args.username))
    if args.command == "set-password":
        password = prompt_new_password()
        if password is None:
            return 1
        return asyncio.run(set_password(args.username, password))
    return 2


if __name__ == "__main__":
    sys.exit(main())
