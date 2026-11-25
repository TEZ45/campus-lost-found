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

## Local team demonstration

The current delivery target is a complete local demonstration and shared source
code. Use the team's frontend with the backend; API tests alone do not establish
that the integrated pages work. The existing Django templates remain available.
Long load tests, full file-capacity tests and production deployment checks can
follow this delivery. Existing validation evidence remains valid for its scope.

Use a separate demo checkout with synthetic data. Do not replace an existing
database, media directory or environment file. Review `.env` against `.env.example`:
use APP_ENV=development, DEBUG=True and DB_ENGINE=sqlite, with no Redis URL or
recovery/operations paths. Existing process environment values take precedence
over `.env`; use fresh terminals without production or test overrides.

In PowerShell, from the demo checkout:

```powershell
$env:APP_ENV = "development"
uv sync --frozen --extra dev
if (!(Test-Path .env)) { Copy-Item .env.example .env }
uv run --frozen --extra dev python manage.py check
uv run --frozen --extra dev python manage.py migrate --noinput
uv run --frozen --extra dev python manage.py createsuperuser
uv run --frozen --extra dev python manage.py runserver 127.0.0.1:8000
```

Open http://127.0.0.1:8000/. In a second terminal in the same checkout:

```powershell
$env:APP_ENV = "development"
uv run --frozen --extra dev python manage.py deliver_notifications
```

Both terminals must use the same database and media configuration. SQLite and
process-local request counters are sufficient for this local demo; PostgreSQL,
the Redis VM, nginx and systemd are not prerequisites. This is not a production
configuration or distributed rate-limit validation. Stop both commands with Ctrl+C.

Prepare separate reporter, claimant and service-staff accounts. Use the local
admin to grant the service account `is_service_staff`; superuser status alone
does not grant business permissions. Use `/staff/` for business actions and
separate browser profiles for different actors. Never commit account passwords
or the generated database/media.

Rehearse the integrated pages in this order:

1. Register/login, publish a found report with a photo, and verify pending visibility.
2. Review as service staff, then search and open the approved report as another user.
3. Submit a claim with private evidence, request more evidence, and resubmit it.
4. Approve with explicit confirmations, start handover, and confirm completion.
5. Verify report closure, competing-claim invalidation, notices and action history.
6. Use another case to demonstrate rejection or withdrawal, and deny unrelated
   users access to evidence and staff operations.
7. Restart the web process and worker; verify saved outcomes and notice delivery.

For a separate frontend, follow [the API contract](api.md): preserve session
cookies and rotated CSRF tokens, fetch `history_url` with pagination, and render
`allowed_actions` with explicit operator confirmations. Coordinate the frontend's
same-origin development proxy with its owner. Do not infer that a separately
developed frontend has been integrated just because the existing templates work.

Prioritize the first week for startup, demo accounts and the complete team
workflow; reserve the second week for integration defects, a repeatable rehearsal
and source submission. Backend fixes stay in the backend branch. Give frontend
owners reproducible issues and expected API behavior instead of changing their
implementation. Team acceptance remains pending until the actual pages are rehearsed.

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
DB_CONN_MAX_AGE bounds WSGI connection reuse to 0..300 seconds, defaulting to
60 in production and 0 in development/tests. Health checks remain enabled.
Budget PostgreSQL connections for all WSGI threads and workers. Set this to 0
if using ASGI instead. See [Django connection management](https://docs.djangoproject.com/en/6.1/ref/databases/#persistent-connections).

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
Shared-IP write and search limits allow the measured 40-RPS metadata workload
across both minute and hour windows. Account, login, registration and actual
multipart upload limits remain tighter and unchanged. File-transfer capacity
is a separate acceptance check; JSON writes do not consume upload counters.

The web entry point is `python scripts/serve.py`, using locked Waitress with a
loopback listener, bounded threads/connections, body/header limits and timeouts.
Use the supplied nginx TLS example with a real certificate. It overwrites proxy
headers, disables upstream retries, aliases only static files, blocks
`/private_media/`, and routes report photos through Django authorization.
The supplied production environment trusts this one local nginx hop; change both
the ingress and settings together for another topology. Never expose port 8000.

## Recovery control

Production requires absolute RECOVERY_STATE_FILE and OPS_STATE_DIR paths outside
the application, media and backups, in separate directories. Recovery control
also stays outside the PostgreSQL data directory and any volume restored with it.
Each web/worker host must see the same current control state. A disconnected or
missing state fails closed; do not distribute independent cached copies.

Create `/var/lib/campus-control/recovery` as root:campus with mode 2750. The
operator owns the state and append-only event files; the campus service group
can read them, but cannot write. New files use mode 0640 and the events directory
inherits the group. On Windows configure equivalent ACLs explicitly. Give the
campus service user its separate writable `/var/lib/campus-operations` directory.

Stop and drain web and worker processes before changing recovery state. The
`--quiesced` flag records an operator assertion, not automatic process discovery.
Use the operator's environment and identity, never expose this command over HTTP:

```text
python manage.py recovery_control pause --operator OPERATOR_ID --reference INCIDENT_ID --quiesced
python manage.py recovery_control status
python manage.py recovery_control resume --operator OPERATOR_ID --reference CUSTODY_REVIEW_ID --quiesced --custody-reconciled
```

Initialize a new deployment as paused. Resume only after authorized staff have
reconciled custody and recorded a review reference. Do not put evidence or personal
details in that reference. API/HTML/model claim transitions and report reviews
are blocked while paused, including confirmation of a stale restored handover.
Read access and new submissions remain available. The gate does not stop a
transaction already in flight: draining processes is mandatory before a restore.
Restoring the business database never restores or automatically resumes this gate.

## Supervision and monitoring

Review the units in `deploy/systemd/` for the actual Linux host before installing
them. They are templates; no host services are installed by repository tests.
They expect `/srv/campus/app`, an existing campus user/group, protected environment
files under `/etc/campus`, existing writable media/operations directories and
an operator-owned recovery directory. Runtime processes use the restricted
database identity. Recovery/backup operator credentials stay outside runtime.env.

Run the web and notification units continuously. Notification polling is one
second. Retention and deletion timers repeat one minute after each bounded batch;
storage reconciliation repeats one hour after completion. Its heartbeat becomes
stale after two hours. Jobs record atomic external heartbeats with only
counts, times and error classes. They preserve the 15/30/45-day policy and report
uncertain cases or unknown legacy finalization dates for manual review.

```text
python manage.py run_maintenance --task retention --batch-size 1000
python manage.py run_maintenance --task deletions --batch-size 1000
python manage.py run_maintenance --task storage --batch-size 1000 --max-files 1000000
python manage.py healthcheck
```

Maintenance commands apply bounded work. `cleanup_retention` and
`reconcile_storage` without `--apply` remain the preview commands. Health checks
are read-only: they never initialize accounting or clean records. A nonzero exit
reports unreachable DB/Redis, notices older than 10 seconds, storage at 80 percent,
failed deletions, cleanup pending for six days, missing legacy clocks, a paused
recovery gate, or missing/stale/failed heartbeats. Seven days marks overdue live
cleanup. Redis monitoring needs PING, INFO memory and CONFIG GET maxmemory-policy;
grant only the CONFIG GET subcommand, never CONFIG SET, to this identity.

The health timer checks every minute. Failed units emit a local system-journal
alert through campus-alert@.service. Connect this signal and JSON status to the
team's actual alert receiver; a journal entry alone does not demonstrate a person
was notified. Do not publish detailed health JSON on a public endpoint. Monthly
availability and incident response still require observation on the real host.

## Protected backups and restoration

Use a dedicated PostgreSQL 17 cluster and matching native backup binaries.
Review `deploy/postgresql-backup.conf.example`; incremental backups need WAL
summarization. The separate backup identity needs LOGIN and REPLICATION, database
CONNECT, membership in pg_read_all_settings, and SELECT on reports_report and
reports_claimattachment. Protect this powerful identity and every physical
backup: a physical backup includes the whole cluster and authentication hashes.
Keep backup storage on a separately protected volume with the deployment's
encryption and access-control policy. Do not mount it into web processes.

Only run snapshots after all application writers and maintenance workers have
stopped and drained. This preserves a consistent database/file set. The backup
CLI refuses a missing referenced file, path overlap, links/junctions, custom
tablespaces, an unsupported PostgreSQL version, or an incomplete dependency chain.

```text
python scripts/backup_snapshot.py backup --root /srv/campus-backups --pg-bin /usr/lib/postgresql/17/bin --public-media /srv/campus/media --private-media /srv/campus/private_media --mode auto --quiesced
python scripts/backup_snapshot.py status --root /srv/campus-backups
python scripts/backup_snapshot.py prune --root /srv/campus-backups
python scripts/backup_snapshot.py prune --root /srv/campus-backups --apply
```

Auto mode starts a full backup each UTC day and otherwise creates an incremental
backup with streamed WAL. Unchanged media may reuse a prior protected backup copy;
live files are never hard-linked into backups. SHA256 inventories cover every
database and media file. Compact metadata keeps routine freshness/dependency
checks separate from payload verification; run `verify` to check file contents.
Older snapshots remain readable. Partial failures are never published as completed
snapshots. Expiry removes entire dependent chains at 14 days from the full backup;
some later increments expire sooner so no dependency is retained past 14 days.
The independent hourly expiry timer does not wait for the next successful backup;
monitor and rerun it after lock conflicts or failures. Status fails for a backup
older than one hour, expired chains, partials or pending deletion retries.

The backup timer runs every 50 minutes to leave time within the one-hour RPO.
It uses a root-owned protected `/var/lib/campus-backup-control` directory. The
scheduled wrapper durably records active writer units, stops timers and writers,
checks that they stopped, and resumes only previously active units. Its finalizer
and systemd ExecStopPost both retry restoration of that service state after a
failure or timeout. An existing writer journal blocks a new backup until resolved.
Expired backup cleanup starts only after writers resume. Children expire before
parents so interrupted cleanup can be retried without orphaning a backup chain.
It never changes the recovery/custody gate. Backup status is checked separately
every minute. Alert on a stuck backup, incomplete snapshot or failed service restart.

This conservative snapshot method briefly stops the web service. Measure the
whole stop-to-restart duration at the real data size before enabling the timer:
the two-hour monthly planned-maintenance budget allows about eight seconds per
50-minute snapshot. If the measured budget is exceeded, a reviewed online or
storage-snapshot method is required before release. Do not treat small local
restore timings as proof that full-capacity backup or availability targets pass.

Recovery sequence:

1. Stop/drain web, workers and their timers. Pause the independent recovery gate.
2. Check backup freshness and the complete chain. Restore only into a new directory:

```text
python scripts/backup_snapshot.py restore --root /srv/campus-backups --snapshot SNAPSHOT_ID --target /srv/campus-restored/INCIDENT_ID --pg-bin /usr/lib/postgresql/17/bin --recovery-state-file /var/lib/campus-control/recovery/state.json --quiesced
```

3. Keep the live directories untouched. The command combines incremental backups,
   runs pg_verifybackup, verifies media hashes and rechecks that collection is paused.
4. Review the restored PostgreSQL listener, credentials, ownership and paths before
   starting it privately. Verify reports, evidence, audit and handover outcomes.
   Reconcile storage and run retention checks before reopening access.
5. Have authorized staff reconcile actual custody, including items transferred
   since the backup. With writers still stopped, record the custody review and
   explicitly resume the gate, then restart services and inspect both health checks.

Record the actual failure point, latest consistent backup, recovery completion,
data checks and custody decision. Targets are RPO at most one hour and RTO at most
four hours; a synthetic local drill establishes tooling behavior, not these
production guarantees. Redis restart/failover, TLS certificates and alert routing
remain separate deployment checks.

## Local load validation

`scripts/load_test.py` requires APP_ENV=test, loopback PostgreSQL/Redis and a new
empty database named test_campus_load_*. It seeds full report/claim counts for the
small, medium and large tiers, then starts owned Waitress and notification workers.
Only those processes are stopped at completion. Never point it at business data.

```text
python scripts/load_test.py --tier small --output /absolute/test-results/small.json
python scripts/load_test.py --tier medium --output /absolute/test-results/medium.json
python scripts/load_test.py --tier large --output /absolute/test-results/large.json
```

Each run uses five minutes of warm-up and fifteen minutes of measurement. The
50/100/200-user tiers offer 10/20/40 RPS, with 70 percent search/detail, 20 percent
report/claim/staff writes and 10 percent progress/notice reads. Sessions are
prepared outside measurement; all users share one IP to exercise campus NAT.
HTTP timing includes generator queue delay. Backpressure, throttling and unexpected
status codes count as failures. Saved writes and notice delivery are also checked.
The WSGI benchmark uses 60-second database connection reuse with health checks,
matching the production default. API action hints batch reservation reads for
the current page; write transactions always recheck current permissions and state.
Short overrides are labeled smoke tests and cannot satisfy the duration gate.

The fixture contains full database record counts but only three small shared
images. Attachment transfer is outside the latency target; the separate 2-MB-per-
image storage/capacity requirement is explicitly unverified by this harness.
Run the full file-capacity and deployment tests on provisioned storage before
claiming URS acceptance. Rerun after changing indexes, topology, limits or workers.

References: [Waitress server arguments](https://docs.pylonsproject.org/projects/waitress/en/stable/arguments.html),
[nginx proxy directives](https://nginx.org/en/docs/http/ngx_http_proxy_module.html),
[PostgreSQL 17 backup and recovery](https://www.postgresql.org/docs/17/continuous-archiving.html).

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
