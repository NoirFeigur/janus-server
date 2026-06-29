"""Seed a super-admin account (role + user + link), idempotent.

Creates, if absent:
- a ``role`` with code ``superadmin`` — this code IS the super-admin marker
  the RBAC layer checks (``auth.constants.SUPERADMIN_ROLE_CODE`` →
  ``AuthenticatedUser.is_superuser``). Holding it bypasses every interface-
  permission gate; the code alone is decisive;
- a ``users`` row (default username ``admin``) with an argon2 password hash;
- the ``user_role`` link binding user to role.

No menu/permission rows are needed: super-admin is the role identity itself, not
an aggregated ``menu -> role_menu -> perm`` chain.

Re-running is safe: existing rows (matched by role code / username, among
non-deleted) are reused, never duplicated. The plaintext password is printed
once at the end — set it explicitly in production.

Usage (from repo root, venv active)::

    python -m scripts.seed_admin
    python -m scripts.seed_admin --username admin --password "S0me-Str0ng-Pass"
"""

from __future__ import annotations

import argparse
import asyncio

from sqlalchemy import select

from src.auth.constants import SUPERADMIN_ROLE_CODE
from src.core.security import hash_password
from src.core.snowflake import next_id
from src.db.models import Role, User, UserRole
from src.db.session import async_session_factory, engine

_SUPERADMIN_ROLE_NAME = "超级管理员"
_DEFAULT_USERNAME = "admin"
_DEFAULT_PASSWORD = "Admin@123456"  # noqa: S105 — dev seed default; override in prod.


async def _seed(username: str, password: str, real_name: str) -> None:
    async with async_session_factory() as session:
        # —— Super-admin role (code is the marker; code-unique among non-deleted) ——
        role = (
            await session.execute(
                select(Role).where(
                    Role.code == SUPERADMIN_ROLE_CODE,
                    Role.is_deleted.is_(False),
                )
            )
        ).scalar_one_or_none()

        if role is None:
            role = Role(
                id=next_id(),
                name=_SUPERADMIN_ROLE_NAME,
                code=SUPERADMIN_ROLE_CODE,
                sort_order=0,
                status="active",
                menu_check_strictly=True,
                remark="系统内置超级管理员角色（code=superadmin，跳过全部接口权限）",
            )
            session.add(role)
            await session.flush()
            print(f"[role]  created superadmin role id={role.id}")
        elif role.status != "active":
            role.status = "active"
            print(f"[role]  reused role id={role.id} (re-activated)")
        else:
            print(f"[role]  reused superadmin role id={role.id}")

        # —— Admin user (username-unique among non-deleted) ——
        user = (
            await session.execute(
                select(User).where(
                    User.username == username,
                    User.is_deleted.is_(False),
                )
            )
        ).scalar_one_or_none()

        created_user = user is None
        if user is None:
            user = User(
                id=next_id(),
                employee_no=username,  # NOT NULL; reuse username for the seed.
                username=username,
                real_name=real_name,
                password=hash_password(password),
                status="active",
                preferred_locale="zh-CN",
                remark="系统内置超级管理员账号",
            )
            session.add(user)
            await session.flush()
            print(f"[user]  created admin user id={user.id} username={username!r}")
        else:
            user.password = hash_password(password)
            user.status = "active"
            print(
                f"[user]  reused admin user id={user.id} username={username!r} "
                "(password reset)"
            )

        # —— Bind user <-> role (unique on (user_id, role_id)) ——
        link = (
            await session.execute(
                select(UserRole).where(
                    UserRole.user_id == user.id,
                    UserRole.role_id == role.id,
                )
            )
        ).scalar_one_or_none()

        if link is None:
            session.add(UserRole(id=next_id(), user_id=user.id, role_id=role.id))
            print(f"[link]  bound user {user.id} -> role {role.id}")
        else:
            print(f"[link]  link already present (user {user.id} -> role {role.id})")

        await session.commit()

        print("\n=== super admin ready ===")
        print(f"  username : {username}")
        if created_user:
            print(f"  password : {password}")
        else:
            print(f"  password : {password}  (reset on this run)")
        print(f"  user id  : {user.id}")
        print(
            f"  role     : {SUPERADMIN_ROLE_CODE} id={role.id} "
            "(code = super-admin marker; 跳过接口权限)"
        )
        print("  请尽快登录后修改默认密码。")

    await engine.dispose()


def main() -> None:
    parser = argparse.ArgumentParser(description="Seed a super-admin account.")
    parser.add_argument("--username", default=_DEFAULT_USERNAME)
    parser.add_argument("--password", default=_DEFAULT_PASSWORD)
    parser.add_argument("--real-name", default="超级管理员")
    args = parser.parse_args()
    asyncio.run(_seed(args.username, args.password, args.real_name))


if __name__ == "__main__":
    main()
