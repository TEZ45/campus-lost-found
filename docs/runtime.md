# Runtime and database setup

The target database is PostgreSQL 17.12 with psycopg 3. SQLite remains a local
development option. Both use the same models and migration history. This change
does not copy, replace or delete an existing SQLite database.

## Reproducible installation

Use Python 3.13 and uv 0.13.9. The lock includes the tested Django 6.1 series.
Update dependencies deliberately, regenerate the lock and repeat validation.

```text
uv lock --check
uv sync --frozen --extra dev
```

Run commands from that environment. On Windows use `.venv/Scripts/python.exe`;
on Linux use `.venv/bin/python`. `uv run --frozen --extra dev` also retains test
dependencies. Do not replace the lock with an environment-wide `pip freeze`.

## Environment selection

Set APP_ENV in the process environment before starting Python:

| Profile | Configuration |
| --- | --- |
| development | Default; reads the root .env without overriding process values; SQLite unless selected otherwise |
| test | Ignores .env; disposable databases and media only |
| production | Ignores .env; requires explicit PostgreSQL, credentials, hosts and shared Redis |

The root [.env.example](../.env.example) is for development. The
[production example](../deploy/production.env.example) contains placeholders
and cannot be used unchanged. Inject production values through the process
supervisor or secret store. Keep administrator and migration credentials off
web, notification and cleanup worker environments.

PostgreSQL uses DB_NAME, DB_USER, DB_PASSWORD, DB_HOST and DB_PORT. DB_SSLMODE
defaults to verify-full in production; provide the trusted CA through libpq's
PGSSLROOTCERT and use the certificate hostname. Production rejects plaintext
modes. Development tests may explicitly use disable on an isolated loopback
server. No connection URL parsing or implicit migration identity is used.

Connect, statement and lock timeouts default to 5 seconds, 15000 ms and 5000 ms.
They bound database waits; they do not establish the URS response-time targets.
Adjust migration command timeouts separately for a reviewed maintenance window.
The web process must not use unlimited waits to accommodate migrations.

Production enables HTTPS redirects and secure session/CSRF cookies. The initial
HSTS duration is one hour. Extending to subdomains or advertising preload requires
explicit SECURE_HSTS_INCLUDE_SUBDOMAINS and SECURE_HSTS_PRELOAD settings. Preload
requires at least one year and all subdomains on HTTPS. Only enable those after
checking the hostname and its subdomains. Django's deployment check reports the
remaining HSTS recommendations until the operator makes this decision.

TRUST_PROXY_HTTPS defaults to false. Enable it only behind an ingress which
overwrites X-Forwarded-Proto and is the only network route to Django. The Django
HTTPS-header setting itself does not filter source addresses. Configure the
actual REQUEST_LIMITS_TRUSTED_PROXIES separately for client-IP accounting.
Cross-origin frontends need explicit HTTPS CSRF_TRUSTED_ORIGINS and a separately
reviewed browser/session deployment; this change does not enable broad CORS.

Use a single shared Redis primary with noeviction, private network access and
authentication/TLS as appropriate. Every worker in one deployment must share
REQUEST_LIMITS_KEY_PREFIX. Other deployments and tests need separate namespaces.
Counter loss after restart/flush/failover remains an operational limitation.

## Separate database identities

For a new database, a database administrator creates two login roles, with
different random passwords assigned through the secret-management procedure:

```sql
CREATE ROLE campus_migrate LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE
    NOINHERIT NOREPLICATION NOBYPASSRLS;
CREATE ROLE campus_runtime LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE
    NOINHERIT NOREPLICATION NOBYPASSRLS;
CREATE DATABASE campus OWNER campus_migrate;
```

Assign passwords before allowing connections. Run `manage.py migrate --noinput`
using DB_USER=campus_migrate and its password. Then run the following as a
separate maintenance process with DB_ADMIN_DSN referring to this database:

```text
python scripts/database_access.py --migration-role campus_migrate --runtime-role campus_runtime
```

The command rejects an elevated runtime role, role memberships and object
ownership. It revokes public database/schema privileges and grants only connect,
schema usage, table DML and sequence usage/read access. Runtime cannot create
temporary tables, alter/drop tables, truncate data or modify django_migrations.
Default privileges cover new tables/sequences created by the migration role.
Rerun the grant command after migrations, especially if a table is recreated.
Use a dedicated database with only the public application schema; review custom
schemas, functions and existing grants separately before applying to an old
shared database. Runtime DML is still powerful: these grants do not replace
object authorization or make application audit protection a database trigger.

Start application processes with DB_USER=campus_runtime and its own password.
Never grant that role membership in campus_migrate or database ownership.

## Validation

Set APP_ENV=test and the selected DB_* variables, then run:

```text
python manage.py check
python manage.py makemigrations --check --dry-run
python manage.py migrate --noinput
python manage.py migrate --check
python -m pytest -q --tb=short --strict-markers
ruff check .
```

PostgreSQL tests need an isolated test account with CREATEDB and schema access.
That test account is not the production runtime identity. Tests create a test_
database; migration tests downgrade and upgrade its schema. Never point the
suite at a production database or use `--reuse-db` on business data.

To validate actual least-privilege behavior, set PG_ROLE_TEST_ADMIN_DSN to an
isolated test cluster and PG_ROLE_TEST_DATABASE=test_campus_roles, then run:

```text
python scripts/check_database_roles.py
```

This creates unique temporary roles and a new database, migrates as the migration
role, exercises handover/notices/all retention stages as runtime, checks denied DDL
and role escalation, and checks future-object grants. It removes only the
database and roles it created. It refuses an existing database of that name.
The standalone runtime_database_smoke.py is an internal test helper, not an
operational maintenance command.

Real Redis tests require REDIS_TEST_URL pointing to a dedicated noeviction
service that permits CONFIG GET. They use unique key prefixes, never FLUSHDB.
If the URL is absent they skip; if configured but unavailable they fail. The
ordinary LocMem fixture cannot substitute for these tests. Run explicitly with:

```text
python -m pytest tests/integration/test_redis_limits.py -q --strict-markers
```

Set TEST_REQUEST_LIMITS_BACKEND=redis to run ordinary workflow requests against
real Redis too. This mode fails at startup when the service is missing. Fault
tests require a separate disposable server and explicit lifecycle controls;
see [Redis validation](redis-validation.md) for setup, scope and actual results.

The GitHub workflow defines SQLite/PostgreSQL jobs using real Redis, with a
second container for fault tests. Both jobs must pass on the current pull request
revision before merging on GitHub. See the
[P2 review](p2-runtime-review.md) for actual results and outstanding release work.
