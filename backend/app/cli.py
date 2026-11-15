"""
NEXUS AI · app/cli.py
Admin bootstrap command. Self-registration always creates `user` accounts,
so the first admin is promoted from the server shell instead of over HTTP.

Usage (inside the api container):
    python -m app.cli make-admin <username>
"""

import argparse
import asyncio
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


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m app.cli")
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("make-admin", help="Give an existing user the admin role")
    p.add_argument("username")
    args = parser.parse_args(argv)

    if args.command == "make-admin":
        return asyncio.run(make_admin(args.username))
    return 2


if __name__ == "__main__":
    sys.exit(main())
