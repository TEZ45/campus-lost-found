# P1 and request protection review

Date: 2026-11-15. Branch: `fix/p1-abuse-and-lifecycle`.
Base: `8adfabaab987eb42a029f17ea07db9e986349ed8`.
At this validation checkpoint, changes were local for review and had not been
committed, merged or pushed. Subsequent publication is recorded in Git history
and the GitHub pull request. Main is not merged locally. The SQL injection
regressions from the earlier local review are included.

## Implemented scope

| Work | Result |
| --- | --- |
| Request admission | API and HTML share atomic IP/account windows and separate login, registration, write, upload and search policies; reject before expensive processing with 429 and Retry-After |
| Cache failure | 503 when accounting fails; DEBUG=False requires the shared Redis backend |
| Proxy identity | Canonical addresses; forwarded headers trusted only from explicitly configured proxies |
| Upload resources | Actual stream cap 17 MiB, aggregate files 15 MiB, per-image 5 MiB, decoded image cap 20 million pixels; existing three/five image limits preserved |
| Business resources | HTML lists/queues/notices paginate; search and text lengths bounded; matching has a shared lease, cooldown, pair cap and cooperative deadline |
| Disputed evidence | Claimant may add evidence privately; dispute state and exclusive reservation remain |
| Reasons and correction | Evidence requests, disputes, withdrawal and closure require reasons; rejected-report owner corrections return to pending review |
| Current instructions | Separate private current-reason fields preserve active instructions after 45-day audit expiry; final evidence cleanup scrubs them |
| Core schema | Non-null protected claimant; event time required for new reports; category suggestions administered through normal Django permissions |
| Data lifecycle | 15-day evidence, 30-day final business data and 45-day history; dry-run, bounded/repeatable cleanup, protected audit detachment and retryable file deletion |
| Storage | Locked byte reservations, 80-percent staff warning, rollback cleanup, orphan reconciliation and pending-delete accounting |

Managed uploads stage complete content before exclusive publication and track
created paths before writing. Partial-write, flush and cleanup failures are
covered. Existing destination files are never overwritten. This local storage
backend requires same-filesystem hard-link support, verified on the test host;
check the deployment filesystem before switching media mounts.

See [schema](schema.md) and [API contract](api.md). Schema migrations are
0007, 0008 and 0009. Legacy category/time values are preserved; missing
claimant identity stops migration with diagnostics instead of deleting or
fabricating records. Historical final clocks are derived only from reliable
recorded outcomes. Unknown clocks remain visible maintenance diagnostics.

## Frontend changes to review

- New report creation and PUT require nonempty category and event_time.
  Campus date/time fields use Asia/Shanghai; unknown legacy time is null.
- Claim withdrawal and report closure requests now require a reason body.
  Staff forms must follow allowed_actions for reason and confirmation inputs.
- Disputed resubmission is allowed without returning the claim to pending.
- Claim responses include current_reason, final/evidence-expiry timestamps and
  handover actor/time. Report review_reason is visible only to its manager.
- 429 includes retry guidance, 413 indicates resource size rejection and 503
  indicates unavailable request accounting. Query saved state before replaying
  a state-changing request whose response was lost.
- Category names remain nonempty text for compatibility. The active catalog
  supplies administrator-managed suggestions rather than rewriting old names.

## Configuring protection

Copy relevant settings from `.env.example`. Single-process local development
may use LocMemCache. Production must set REQUEST_LIMITS_REDIS_URL to one shared
Redis primary, preserve the common key namespace and disable eviction. A flush
or restart resets fixed-window budgets; primary failover policy requires
deployment validation. IP identity must be configured from verified proxy hops.

| Default budget | Requests |
| --- | --- |
| IP global | 3000/minute and 30000/hour |
| Account global | 300/minute and 3000/hour |
| Account login | 30/minute and 100/hour |
| Account registration | 10/minute and 100/hour |
| Account writes | 60/minute and 600/hour |
| Account uploads | 20/minute and 200/hour |
| Account search | 120/minute and 1200/hour |

IP endpoint caps multiply the account endpoint values by 20 to accommodate
shared campus NAT. Anonymous login/registration still have those IP caps.
These are configurable starting points, not measured capacity guarantees.
Two adjacent fixed windows can allow twice one window's allowance; independent
short and long windows both apply. The Redis operations used for admission
must be atomic and shared across workers.

Matching defaults to 2500 pairs, a 10-second cooperative budget and a
30-second cooldown. Deadline checks surround preprocessing and pair work;
they cannot interrupt a blocked database statement. Configure target-database
statement/lock timeouts and measure real worker behavior before deployment.

The [NGINX example](../deploy/nginx-request-limits.conf.example) supplies ingress
body, connection, rate and timeout controls. It preserves three 5 MiB uploads.
It is a template requiring TLS, actual network topology and capacity tuning;
the Django listener must remain private. ASGI can buffer before middleware,
so enforce total body limits upstream. Application throttling does not absorb
an attack that saturates the server's network link.

## Maintenance and safety

Run from the project directory, using `.venv/Scripts/python.exe`:

```text
manage.py cleanup_retention --batch-size 100
manage.py reconcile_storage --max-files 10000
```

Both commands default to read-only previews. After reviewing the output and
backups, `--apply` enables bounded work. Schedule frequent repeated passes,
monitor pending age and failed file jobs, and verify the required seven-day
live cleanup window. Queueing or failing a deletion does not release its byte
charge. Completed deletion jobs discard original filenames; old completed
jobs expire after 45 days. Reconciliation recounts both managed roots and
queues unreferenced files, excluding backups and symlinks. Initialize/recount
usage before deployment and set capacity to the actual attachment byte budget;
the default is 100 GiB, excluding backups.

Expiry starts from finalized_at/closed_at, never a mutable updated_at. Active
evidence is not erased. Dispute/uncertain-handover holds retain evidence for
the same item. Business records are not cascaded away while newer/active
children survive. Ordinary audit update/delete protections remain enabled;
only the internal maintenance path detaches or expires protected references.
Copied freeform audit text, current reasons and matching legacy notice bodies
are scrubbed alongside evidence; decision facts remain until history expiry.

The local database was backed up to
`E:/SE/local-backups/2026-11-15-p1/before-p1-migrations.sqlite3` before applying
migrations. Local cleanup/reconciliation ran in dry-run mode only. Preview
found no eligible records or missing legacy final clocks, plus 12 unreferenced
files totaling 850 bytes; these files were not deleted or queued.

## Validation

| Check | Final result |
| --- | --- |
| Full suite, including real loopback HTTP | 605 passed in 119.59 seconds |
| Ruff | Passed |
| Django system check | No issues |
| Migration consistency | No changes detected |
| Local database migration | Applied through 0009; no unapplied migrations |
| Fresh/upgrade schema and legacy-data checks | Included in passing suite |
| Whitespace and changed-file character checks | Passed; ASCII only, no tabs |

Tests use isolated databases/media and per-test counters; protection remains
enabled. Production-mode tests explicitly verify unsupported caches fail.
Concurrency tests include competing quota reservations, request-counter
admission, approval/reservation races and notice workers. Failure tests cover
partial disk writes, rollback cleanup, durable deletion retry and notification
failure after business commit. Tests also exercise 15/30/45-day boundaries,
active instructions after audit expiry and legacy notice proof scrubbing.

The first integration runs exposed old successful request fixtures that lacked
the now-required category, event time or reasons. Those fixtures now submit
the required fields; rejection checks remain enabled. Final full-suite results
above reflect the completed code, including partial-write and current-reason fixes.

## Remaining deployment and acceptance work

- Real shared Redis and NGINX execution were not available in this environment.
  Atomic-counter concurrency and failure behavior have automated tests; actual
  Redis failover/eviction and `nginx -t` still require the deployment environment.
- Complete target-database, capacity-tier/load, external frontend integration
  and manual A1-A6 acceptance. This work does not certify production latency
  or resilience under volumetric DDoS.
- Configure TLS/secrets/secure cookies, production worker limits, shared Redis,
  trusted proxy networks and upstream DDoS protection before public exposure.
- Enable and monitor live cleanup after approval; configure 14-day protected
  backup expiry, restore drills and custody reconciliation as P2 operations.
  After an unclean process/machine stop, reconcile disk usage and orphan
  staging files before reopening uploads; in-process compensation cannot run
  after a hard kill or power loss.
- External legacy data with unknown owner/final timestamps requires verified
  remediation. Commands report and preserve those rows rather than guessing.
- Dependency locking, production DDL privilege separation and recovery-pause
  controls remain the documented P2 work. GitHub Issue 1 itself is not updated
  or closed by these local code changes.
