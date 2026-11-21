"""Verify roles in a new disposable test database, then remove only that database."""

import os
import re
import secrets
import subprocess
import sys
from pathlib import Path

import psycopg
from database_access import grant_runtime_access
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict

ROOT = Path(__file__).resolve().parents[1]


def environment(connection, role, password):
    return {
        **{key: value for key, value in os.environ.items()
           if key not in {"PG_ROLE_TEST_ADMIN_DSN", "DB_ADMIN_DSN"}},
        "APP_ENV": "test",
        "DB_ENGINE": "postgresql",
        "DB_NAME": connection["dbname"],
        "DB_HOST": connection.get("host", "127.0.0.1"),
        "DB_PORT": str(connection.get("port", 5432)),
        "DB_USER": role,
        "DB_PASSWORD": password,
        "DB_SSLMODE": connection.get("sslmode", "prefer"),
    }


def check_denied(connection, statement):
    try:
        with connection.transaction():
            connection.execute(statement)
    except psycopg.errors.InsufficientPrivilege:
        return
    raise AssertionError("Runtime role was allowed a forbidden operation.")


def main():
    admin = conninfo_to_dict(os.environ["PG_ROLE_TEST_ADMIN_DSN"])
    database = os.getenv("PG_ROLE_TEST_DATABASE", "test_campus_roles")
    if not re.fullmatch(r"test_[a-z0-9_]{1,45}", database):
        raise ValueError("Use a new test_ database, never an existing application database.")
    suffix = secrets.token_hex(6)
    migration_role, runtime_role = f"clfm_{suffix}", f"clfr_{suffix}"
    passwords = {role: secrets.token_urlsafe(32) for role in (migration_role, runtime_role)}
    created_roles = []
    created_database = False
    with psycopg.connect(**admin, autocommit=True) as cluster:
        if cluster.execute("SELECT 1 FROM pg_database WHERE datname = %s", (database,)).fetchone():
            raise ValueError("Refusing to reuse or remove an existing database.")
        try:
            for role in (migration_role, runtime_role):
                cluster.execute(sql.SQL(
                    "CREATE ROLE {} LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE "
                    "NOINHERIT NOREPLICATION NOBYPASSRLS PASSWORD {}"
                ).format(sql.Identifier(role), sql.Literal(passwords[role])))
                created_roles.append(role)
            cluster.execute(sql.SQL("CREATE DATABASE {} OWNER {}").format(
                sql.Identifier(database), sql.Identifier(migration_role),
            ))
            created_database = True
            target = {**admin, "dbname": database}
            subprocess.run(
                [sys.executable, "manage.py", "migrate", "--noinput", "--verbosity", "0"],
                cwd=ROOT, env=environment(target, migration_role, passwords[migration_role]),
                check=True, timeout=120,
            )
            with psycopg.connect(**target) as owner:
                grant_runtime_access(owner, migration_role, runtime_role)
            runtime = {**target, "user": runtime_role, "password": passwords[runtime_role]}
            with psycopg.connect(**runtime, autocommit=True) as worker:
                for statement in (
                    "CREATE TABLE public.forbidden (id integer)",
                    "CREATE SCHEMA forbidden",
                    "CREATE TEMP TABLE forbidden (id integer)",
                    "ALTER TABLE public.reports_report ADD COLUMN forbidden integer",
                    "DROP TABLE public.reports_category",
                    "TRUNCATE public.reports_category",
                    "DELETE FROM public.django_migrations",
                    sql.SQL("SET ROLE {}").format(sql.Identifier(migration_role)),
                ):
                    check_denied(worker, statement)
                row = worker.execute(
                    "SELECT rolsuper, rolcreatedb, rolcreaterole, rolreplication, rolbypassrls "
                    "FROM pg_roles WHERE rolname = current_user"
                ).fetchone()
                assert row == (False,) * 5
                assert not worker.execute(
                    "SELECT 1 FROM pg_auth_members WHERE member = "
                    "(SELECT oid FROM pg_roles WHERE rolname = current_user)"
                ).fetchone()
            subprocess.run(
                [sys.executable, str(ROOT / "scripts" / "runtime_database_smoke.py")],
                cwd=ROOT, env=environment(target, runtime_role, passwords[runtime_role]),
                check=True, timeout=120,
            )
            with psycopg.connect(
                **{**target, "user": migration_role, "password": passwords[migration_role]},
            ) as migrator:
                migrator.execute("CREATE TABLE public.next_migration (id bigserial PRIMARY KEY)")
                migrator.execute("ALTER TABLE public.next_migration ADD COLUMN note text")
            with psycopg.connect(**runtime) as worker:
                worker.execute("INSERT INTO public.next_migration (note) VALUES ('grants verified')")
                assert worker.execute("SELECT count(*) FROM public.next_migration").fetchone() == (1,)
            print("Role checks passed: business workflow, future grants, DDL and role escalation denied.")
        finally:
            if created_database:
                cluster.execute(sql.SQL("DROP DATABASE {} WITH (FORCE)").format(
                    sql.Identifier(database),
                ))
            for role in reversed(created_roles):
                cluster.execute(sql.SQL("DROP ROLE {}").format(sql.Identifier(role)))


if __name__ == "__main__":
    main()
