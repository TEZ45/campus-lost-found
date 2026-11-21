# Real Redis validation

Initial validation: 2026-11-19. Branch: feat/p2-runtime-baseline.
Publication review started on 2026-11-21;
the GitHub pull request records remote checks and merge status.

## Database decision

The host has MySQL Community 9.3.0 and a running MySQL93 service. Read-only
inspection found InnoDB and strict SQL mode. No existing MySQL database or
service was changed.

Keep PostgreSQL 17.12 for this project. Claim's one_reserved_claim_per_report
constraint is a conditional unique index over the three reservation states.
MySQL ignores conditional indexes; Django warns that this unique constraint
will not be created. InnoDB row locks can support the service checks, but an
unchanged database switch would remove the independently tested database guard.
A MySQL implementation would need a different reservation constraint/schema and
new migration/concurrency validation. MySQL 9.3 is also an Innovation release;
a new MySQL baseline should use a supported LTS release if the team chooses it.

References: [Django conditional indexes](https://docs.djangoproject.com/en/6.1/ref/models/indexes/#condition),
[conditional uniqueness](https://docs.djangoproject.com/en/6.1/ref/models/constraints/#django.db.models.UniqueConstraint.condition),
and [MySQL release policy](https://dev.mysql.com/doc/refman/9.7/en/mysql-releases.html).
Redis supplies shared request counters; it does not replace the business database.

## Local environment

The host is Windows. Django and PostgreSQL run natively on Windows. Real Redis
7.4.12 runs in a portable QEMU 11.1 Alpine Linux guest under the already available
WHPX accelerator, with 2 vCPUs, 768 MiB RAM and a 2 GiB sparse disk. No Windows
feature, system service, dual boot or host restart was needed. The runtime files
and protected credentials remain outside Git in E:/SE/p2-runtime/redis-env.
Its README.txt documents reusable startup and shutdown commands.

Only loopback ports are exposed. A persistent SSH tunnel forwards host ports
57379 and 57380 to the normal and fault Redis servers. Direct QEMU networking
showed intermittent connection timeouts; the tunnel avoids repeated NAT setup.
Both servers require authentication and use noeviction. RDB and AOF are disabled
for these disposable counters. Application connect/read timeouts remain one
second. This local test topology is not a production deployment recommendation.

## Test isolation

TEST_REQUEST_LIMITS_BACKEND=redis makes ordinary workflow requests use actual
Redis instead of the default LocMem test fixture. Startup fails if REDIS_TEST_URL
is missing, unavailable or not configured with noeviction. Each test has a unique
key prefix. Cleanup removes only that prefix and never calls FLUSHDB or FLUSHALL.
Explicit counter unit tests and the unsupported-cache rejection test still
select their intended fake or LocMem backend.

Django RedisCache.close does not disconnect its pools. Repeated CACHES overrides
in tests can abandon those pools, so teardown explicitly disconnects all Django
Redis pools created during that test, including replaced cache instances. This
leaves within-test reconnect behavior intact and changes no production settings.

The five normal Redis integration cases check:

- Exactly 13 admissions across 80 attempts from four separate processes.
- Shared HTML/API limits before invalid login input is parsed.
- Counter expiry and increments that do not extend the original TTL.
- Account limits across source IPs without blocking another account.
- Real refused connections reject before business work, followed by recovery.

Two additional fault cases use a different, empty server:

- noeviction with an exhausted memory budget returns 503, preserves an existing
  counter, then resumes after restoring the memory limit.
- An actual stop/start returns 503 during the outage and reconnects through the
  same Django cache instance. With RDB/AOF disabled, restart loses the old budget;
  the test explicitly records this limitation rather than treating it as durability.

## Running the tests

Use the locked environment described in [runtime.md](runtime.md). Set these in
the process environment, never commit passwords:

```text
APP_ENV=test
TEST_REQUEST_LIMITS_BACKEND=redis
REDIS_TEST_URL=redis://127.0.0.1:57379/1
REQUEST_LIMITS_REDIS_URL=redis://127.0.0.1:57379/0
REDIS_FAULT_TEST_URL=redis://127.0.0.1:57380/0
REDIS_ALLOW_FAULT_TESTS=yes
```

Supply Redis authentication in the URLs when required. The normal server must
use noeviction. The fault URL must refer to a different loopback server, empty
across all databases, with noeviction and no AOF. Fault tests change maxmemory
and stop/start that server; never point them at a shared or production instance.
REDIS_FAULT_STOP_COMMAND and REDIS_FAULT_START_COMMAND are JSON arrays of trusted
local command arguments. They are executed without a shell. The start command
must also succeed when the service is already running. Configuration is restored
in cleanup, and restart is attempted even if stopping fails partway.

Run one suite at a time when sharing the fault server:

```text
python -m pytest -q --tb=short --strict-markers
```

The workflow defines separate normal/fault Redis containers for each database
job and uses docker stop/start on only the fault container. Both jobs must pass
on the pull request's current revision before merging on GitHub.

## Results

Executed with Python 3.13.9, Django 6.1.3, redis-py 8.1.0, Redis 7.4.12 and
PostgreSQL 17.12. The two database suites run sequentially against isolated
databases/media and the normal/fault Redis servers.

| Check | Result |
| --- | --- |
| SQLite full suite with real Redis | 661 passed, 2 PostgreSQL-only tests skipped in 160.98 seconds |
| PostgreSQL full suite with real Redis | 663 passed, no skips in 186.67 seconds |
| PostgreSQL migrations and model drift | Passed; no pending migration or model change |
| Runtime database role | Business workflow, notice delivery, retention cleanup and future grants passed; forbidden DDL/role escalation denied |
| Production configuration | check --deploy --fail-level WARNING passed; this does not establish a production TLS connection |
| Ruff, lock consistency, whitespace, ASCII and no-tabs checks | Passed |
| GitHub workflow at the initial checkpoint | Locally defined; remote results are recorded on the publication pull request |

The initial full-suite attempt exposed abandoned test cache pools and exhausted
the Windows SSH forwarder's file handles. Redis itself did not crash. The final
run uses explicit per-test pool cleanup; application timeouts were not relaxed,
and Redis failure checks were not skipped. The failed-run XML is retained beside
the final logs for review.

Periodic resource samples across both November 19 suites showed 1-7 connected clients,
including the probe, returning to one between activity bursts. No blocked or
rejected clients were observed. These samples are not a continuous peak measure.
There were 34 samples across 6 minutes 39 seconds, covering 2,078 accepted
connections; the final sample returned to one client.
All seven Redis integration/fault cases executed in both suites. The two SQLite
skips were the PostgreSQL statement-timeout and row-lock-timeout cases; both
passed on PostgreSQL.

The standalone transport probe passed 2,000 independent authenticated connections
with eight workers and a maximum observed round-trip of 0.0572 seconds. That
probe closed each client and did not expose abandoned test pools; it is not a
substitute for the full suites or production capacity testing.

JUnit XML, full-suite logs, database-verification.log and production-check.log
remain in E:/SE/p2-runtime outside Git. Test runtime is not request latency.
After validation, the Redis guest, SSH tunnel and portable PostgreSQL cluster
were shut down cleanly. Their test ports no longer listen. Reusable runtime
files remain local; the existing MySQL93 service is still running unchanged.

## Publication review

The November 21 review moved fault-service lifecycle commands into the workflow
step environment, where GitHub permits the job.services context. The TTL test
now observes a reduced lifetime before incrementing, so resetting the TTL cannot
pass merely because two immediate readings happen in the same millisecond.

Both full suites were repeated with real Redis after these changes:

| November 21 check | Result |
| --- | --- |
| SQLite and real Redis | 661 passed, 2 PostgreSQL-only skips in 331.19 seconds |
| PostgreSQL and real Redis | 663 passed, no skips in 338.27 seconds |
| Redis normal/fault cases | All seven passed in each database suite |
| Migration drift/status, runtime role and retention workflow | Passed |
| Production configuration, Ruff, lock, whitespace and ASCII | Passed |

The nov21-* logs and JUnit files in E:/SE/p2-runtime preserve this publication
run separately from the initial evidence. Remote CI results are recorded on the
GitHub pull request and must pass before the remote merge.

## Remaining work

1. Validate the real deployment's Redis TLS/ACL, memory sizing, persistence and
   replica failover. The tested volatile restart resets existing rate budgets;
   deployment durability and failover policy remain undecided.
2. Exercise the actual ingress, trusted proxy configuration, process limits and
   shared campus NAT traffic. Measure the URS 50/100/200-user and 10/20/40-RPS
   tiers; application limits alone cannot protect a saturated network.
3. Complete backup/restore drills and restore-time collection suspension outside
   the restored database, with physical custody reconciliation.
4. Supervise notification workers and schedule/monitor retention, deletion
   retries and storage reconciliation. Retention remains 15/30/45 days.
5. Complete frontend integration and A1-A6 team acceptance, and resolve unknown
   legacy ownership/final clocks before any actual data cutover.
