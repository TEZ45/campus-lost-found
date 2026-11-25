# P2 runtime baseline review

Date: 2026-11-19. Branch: feat/p2-runtime-baseline.
Base: 356334081b1e41c8605b66d3a86bdf0bdbdcfdeb, the remotely merged P1.
Initial validation was local. Publication was authorized on 2026-11-21 and uses
a feature-branch push and GitHub pull request, with remote checks before merge.
Read-only verification on that date confirmed main still pointed to this base.
The subsequent [real Redis review](redis-validation.md) records the Redis
environment, full-suite cache mode and database comparison. The initial test
checkpoint below predates that follow-up.
The local 2026-11-24 follow-up is recorded at the end of this document.

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

## Remaining work at the initial checkpoint

This historical list predates the local follow-up below. See the final section
for the remaining deployment acceptance gates.

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

## Local follow-up on 2026-11-24

Branch: fix/p2a-claim-history-pagination. At this checkpoint the changes were
uncommitted locally; no merge, push, remote CI run or production deployment had
been performed. The delivery scope below records the subsequent publication plan.

Claim history now has an authorized paginated endpoint, with a default of 20
and a maximum of 100 entries. Claim responses expose history_url instead of
embedding all history. Detail and history reads authorize before related data
loading. Action hints batch reservation reads for the current page; business
transactions still enforce current ownership, state and reservation constraints.

The operational package adds independent recovery control, tracked maintenance,
health checks, protected PostgreSQL full/incremental backups, safe new-target
restoration, retryable expiry and deployment/worker/timer examples. Recovery
requires explicit quiescence and custody reconciliation. Live retention remains
15/30/45 days and protected backup retention remains 14 days.

WSGI connections use bounded reuse with health checks. Shared-IP read/write
budgets support the campus metadata workload. JSON writes no longer consume
file-upload counters. Account, login, registration and multipart upload limits
remain unchanged. Routine backup checks use compact metadata; restore and explicit
verification retain full payload checksums. Failed expiry uses retryable deletion
directories and cannot block creation of a new backup solely by losing metadata.

Final local regression used real Redis, including all seven integration/fault
cases in each database run:

| Database | Passed | Skipped | Failures |
| --- | --- | --- | --- |
| SQLite | 947 | 2 PostgreSQL-only cases | 0 |
| PostgreSQL | 949 | 0 | 0 |

Ruff, dependency-lock consistency, ASCII/space checks, migration checks, fresh
SQLite migrations, production configuration and disposable database-role checks
passed. The runtime role completed workflows and retention cleanup while DDL
and role escalation remained denied. No model migration is required.

A fresh PostgreSQL 17 drill verified native full/incremental backups, media
hashes, audit records, latest-state restoration, stale-state collection blocking,
explicit custody resume, interrupted expiry retries and legacy backup compatibility.
The final tiny-fixture full backup took 6.75 seconds, incremental backup 5.54
seconds, and restoration/startup/data verification 20.02 seconds. These are
tooling measurements, not production RPO/RTO certification.

Local nginx/TLS validation passed seven ingress checks and 23 actual media
authorization requests. Existing private evidence could not bypass the protected
API/HTML routes. This did not verify production database TLS or real certificates.

All three final tiers used the same final source and configuration, five minutes
of warm-up and fifteen minutes of measurement:

| Users | Reports / claims | Achieved RPS | Read p95 | Write p95 | Notice p95 | Errors |
| --- | --- | --- | --- | --- | --- | --- |
| 50 | 5000 / 10000 | 10.000 | 0.087 s | 0.115 s | 0.980 s | 0 |
| 100 | 10000 / 20000 | 20.000 | 0.090 s | 0.105 s | 0.991 s | 0 |
| 200 | 20000 / 40000 | 39.997 | 0.126 s | 0.145 s | 0.993 s | 0 |

The 50/100-user tiers ran concurrently on the same local host; the final
PostgreSQL regression overlapped early warm-up only, not measurement. The
200-user tier used the ordinary entry point in isolation. Each tier reached
its user target with no warm-up errors, no pending notices and no mismatches
between acknowledged writes and stored business/audit records. The respective
write counts were 2400, 4800 and 9600, including warm-up. Every owned web and
notification process stayed alive until cleanup and was stopped afterward.

Final evidence files are nov24-p2-verified-small.json,
nov24-p2-verified-medium.json and nov24-p2-isolated-large.json.

Earlier evidence is retained: a simultaneous three-tier run failed at the large
tier with server queue saturation and upload-budget rejections. A diagnostic
run with thread-dump instrumentation exited unexpectedly; its cause was not
established and it is not acceptance evidence. The ordinary final large-tier
run stayed alive throughout. No timeouts or thread limits were raised to pass.

Local logs and JSON/JUnit records remain under E:/SE/p2-runtime. No credentials,
database files, backups, VM images or private test artifacts are in the repository.
The final validation JSON records file hashes and evidence paths. PostgreSQL,
the Redis test VM, its tunnel and all owned HTTP workers were stopped after
verification; their test listeners were confirmed closed.

## Deployment acceptance still required

- Validate 100/200/400 GiB file capacity on provisioned storage. Local capacity
  fixtures use full database row counts but three small shared images; the local
  disk has only about 40 GiB free.
- Measure whole backup stop-to-restart time at real data size. The conservative
  method stops writers and verifies parent payloads. The two-hour monthly
  maintenance target permits only about eight seconds per 50-minute snapshot;
  an online or storage-snapshot design may be needed before enabling the timer.
- Validate real-host systemd execution, alert delivery, TLS/ACL, Redis persistence
  and failover, backup storage protection, RPO/RTO and custody reconciliation.
- The frontend still needs history_url integration and legacy HTML history
  pagination. Team-owned frontend, presentation and acceptance documents were
  not changed in this follow-up.

## Delivery scope on 2026-11-25

The owner prioritized a complete team demonstration on a local machine and source
submission within two weeks. Further long load tests, full file-capacity tests
and production deployment acceptance are deferred; they are not marked passed.
Keep authorization, evidence privacy, explicit handover confirmations and the
15/30/45-day retention rules unchanged.

The validated backend changes are intended for the shared feature branch
fix/p2a-claim-history-pagination, without merging main. Local databases, media,
credentials and test environments remain outside the published source. The
workflow triggers on pull requests, main pushes or manual dispatch; a feature
branch push alone does not establish a remote CI result.

The [local demo procedure](runtime.md#local-team-demonstration) covers startup,
roles, the notification worker and a cross-role rehearsal. It is an acceptance
checklist, not a claim that the team's actual frontend has already passed an
end-to-end browser rehearsal. Frontend owners retain their implementation work.

The pre-publication core regression passed 350 tests in 101.39 seconds using
SQLite and process-local request counters. It covered HTTP sessions, reports,
claims, handover, confirmations, notices, history pagination and access control.
Source hashes matched the 2026-11-24 full validation; only backend documentation
changed afterward. No additional load test or real Redis run was needed for
these documentation changes.
