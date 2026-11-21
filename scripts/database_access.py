"""Grant application DML access using a separate database administrator."""

import argparse
import os

import psycopg
from psycopg import sql


def grant_runtime_access(connection, migration_role, runtime_role):
    if migration_role == runtime_role:
        raise ValueError("Migration and runtime roles must differ.")
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT oid, rolsuper, rolcreatedb, rolcreaterole, rolreplication, rolbypassrls "
            "FROM pg_roles WHERE rolname = %s", (runtime_role,),
        )
        runtime = cursor.fetchone()
        if runtime is None or any(runtime[1:]):
            raise ValueError("Runtime role must exist without elevated attributes.")
        cursor.execute("SELECT 1 FROM pg_auth_members WHERE member = %s", (runtime[0],))
        if cursor.fetchone():
            raise ValueError("Runtime role must not inherit or switch to other roles.")
        cursor.execute(
            "SELECT 1 FROM pg_database WHERE datdba = %s "
            "UNION ALL SELECT 1 FROM pg_namespace WHERE nspowner = %s "
            "UNION ALL SELECT 1 FROM pg_class WHERE relowner = %s "
            "UNION ALL SELECT 1 FROM pg_proc WHERE proowner = %s",
            (runtime[0],) * 4,
        )
        if cursor.fetchone():
            raise ValueError("Runtime role must not own database objects.")
        cursor.execute("SELECT current_database()")
        identifiers = {
            "database": sql.Identifier(cursor.fetchone()[0]),
            "migration": sql.Identifier(migration_role),
            "runtime": sql.Identifier(runtime_role),
        }
        statements = (
            "REVOKE ALL ON DATABASE {database} FROM PUBLIC, {runtime}",
            "GRANT CONNECT ON DATABASE {database} TO {migration}, {runtime}",
            "REVOKE ALL ON SCHEMA public FROM PUBLIC, {runtime}",
            "GRANT USAGE, CREATE ON SCHEMA public TO {migration}",
            "GRANT USAGE ON SCHEMA public TO {runtime}",
            "REVOKE ALL ON ALL TABLES IN SCHEMA public FROM {runtime}",
            "GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO {runtime}",
            "REVOKE ALL ON ALL SEQUENCES IN SCHEMA public FROM {runtime}",
            "GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO {runtime}",
            ("ALTER DEFAULT PRIVILEGES FOR ROLE {migration} IN SCHEMA public "
             "GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO {runtime}"),
            ("ALTER DEFAULT PRIVILEGES FOR ROLE {migration} IN SCHEMA public "
             "GRANT USAGE, SELECT ON SEQUENCES TO {runtime}"),
            "REVOKE INSERT, UPDATE, DELETE ON public.django_migrations FROM {runtime}",
        )
        for statement in statements:
            cursor.execute(sql.SQL(statement).format(**identifiers))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--migration-role", required=True)
    parser.add_argument("--runtime-role", required=True)
    args = parser.parse_args()
    with psycopg.connect(os.environ["DB_ADMIN_DSN"]) as connection:
        grant_runtime_access(connection, args.migration_role, args.runtime_role)
    print("Runtime grants applied. Keep administrator and migration credentials off app workers.")


if __name__ == "__main__":
    main()
