# P2 runtime baseline review

Date: 2026-11-19. Branch: feat/p2-runtime-baseline.
Base: 356334081b1e41c8605b66d3a86bdf0bdbdcfdeb, the remotely merged P1.
Initial validation was local. Publication was authorized on 2026-11-21 and uses
a feature-branch push and GitHub pull request, with remote checks before merge.
Read-only verification on that date confirmed main still pointed to this base.
The subsequent [real Redis review](redis-validation.md) records the Redis
environment, full-suite cache mode and database comparison. The initial test
checkpoint below predates that follow-up.

## Requirements review

Reviewed all 36 slides and speaker notes of the supplied Combined Presentation.
Core roles, private evidence, reservation, dispute and handover rules agree with
the implemented P1. Slides 19 and 28 explicitly describe logical analysis rather
than a mandatory physical schema. Existing claim constraints, handover fields
and audit records implement those concepts without introducing duplicate tables.

Slides 12 and 33 retain older 90/365/730-day values. The owner confirmed on
2026-11-19 that the approved policy remains evidence 15 days after final outcome,
closed reports/final claims 30 days, and history 45 days. No runtime policy was
reverted. Active-case holds, the seven-day live deletion window and fourteen-day
protected backup expiry remain unchanged.

A corrected presentation copy was saved locally, including the slide 12 notes.
The original and every unrelated package part were preserved. Both affected
slides were rendered and inspected. The supplied original already contains an
incomplete chart relationship; that unrelated issue remains in the copy.

The report also leaves public labels, changed-report visibility, ownership
criteria/custody procedure and supported browsers open for team review. Current
behavior keeps changed public fields pending moderation; the API contract remains
the implementation reference. No new campus policy is inferred from those open
points.

## Implemented scope

- PostgreSQL 17.12 target with psycopg 3; SQLite remains a development option.
- Separate environment profiles and fail-fast production configuration.
- Bounded database connection, statement and lock waits.
- Locked dependencies, clean-install verification and a CI definition.
- Separate migration/runtime identities, grants and disposable permission checks.
- PostgreSQL statement/lock timeout checks and existing concurrency regressions.
- Explicit real Redis integration tests, isolated from ordinary LocMem fixtures.

Setup and operational boundaries are in [runtime.md](runtime.md). Existing local
SQLite data and uploaded files are not migrated or cleaned by this change.

## Initial validation checkpoint

| Check | Result |
| --- | --- |
| Fresh frozen install | Passed; Python 3.13.9, uv 0.13.9, Django 6.1.3, psycopg 3.3.9 |
| Lock consistency and offline frozen sync | Passed |
| SQLite full suite | 654 passed, 5 skipped in 235.89 seconds |
| PostgreSQL 17.12 full suite | 656 passed, 3 skipped in 275.48 seconds |
| Both databases: fresh migrations, migration drift and migration status | Passed through 0009; no new model migration needed |
| Historical upgrade/invalid legacy data checks | Passed in both full suites |
| Runtime role and future-object grants | Passed in a separate fresh PostgreSQL database |
| Runtime business workflow | Handover, actual notice delivery and 15/30/45-day cleanup passed |
| Forbidden runtime operations | CREATE TABLE/SCHEMA/TEMP, ALTER, DROP, TRUNCATE, migration-history DELETE and SET ROLE denied |
| Production configuration check | check --deploy --fail-level WARNING passed with explicit HSTS opt-in; no production connection claimed |
| Ruff, whitespace, ASCII and no-tabs checks | Passed |
| Real Redis integration | 3 skipped; no supported local Redis service |
| GitHub workflow | Defined and locally inspected, not executed remotely |

SQLite additionally skips the two PostgreSQL-only statement and row-lock timeout
tests. Those two tests passed on PostgreSQL. All other skips are the explicit
real Redis tests; ordinary LocMem tests are not counted as Redis validation.

The first SQLite run inside the execution sandbox recorded 69 failures caused
by denied hard links and loopback socket access. A focused reproduction confirmed
WinError 5 at os.link and the HTTP failures reported WinError 10013. The final
full rerun used normal local test permissions and passed without changing business
code or disabling those checks. Both backends used disposable databases/media.
The PostgreSQL instance used a separate loopback-only portable test cluster;
no system service or existing business database was changed.

JUnit results and the retained sandbox-failure record are in the local
E:/SE/p2-runtime directory, outside the repository. These timing values describe
test-suite execution, not production request latency. Redis was unverified at
this initial checkpoint; see the follow-up review for current local results.
The publication pull request records remote CI results. Deployment acceptance
remains unverified.

## Remaining P2 work

1. Validate Redis on the actual deployment, including TLS/ACL, persistence and
   replica failover. Local tests cover shared counters, noeviction memory failure,
   outages and a volatile restart.
2. Configure and test the real ingress, TLS, proxy headers, process limits,
   protected media mounts and same-filesystem hard-link support.
3. Implement restore-time collection suspension independent of the restored
   business database; perform backup/restore and physical custody reconciliation
   drills for RPO <= 1 hour and RTO <= 4 hours.
4. Supervise notice workers and schedule/monitor retention, deletion retries and
   storage reconciliation, including the seven-day live cleanup window and
   fourteen-day protected backup expiry.
5. Measure the 50/100/200-user and 10/20/40-RPS tiers against URS latency/error
   targets; tune limits for shared campus NAT. An application rate limit does
   not protect a saturated network connection.
6. Finish frontend integration and record A1-A6 team acceptance. Resolve unknown
   legacy ownership/final clocks with evidence before an actual data cutover.

The local database switch is a configuration capability, not a completed
production migration or a claim that deployment and acceptance gates have passed.
