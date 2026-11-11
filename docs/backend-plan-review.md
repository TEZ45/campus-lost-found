# Backend plan for independent review

Review date: 2026-11-10.
Baseline: `ab3724a1049c823757367f653f4a3e9908af426b`, branch `fix/p1-backend`.
Source requirement: Campus Lost and Found System URS, version 1.0,
17 October 2026, especially Sections 6.4, 6.8, 6.9 and 8, DOM-1 to DOM-5,
and acceptance checks A1 to A6. Issue 1 covers basic models and migrations.

Retention update supplied by the user on 2026-11-11: private evidence is kept
for 15 days after a final outcome, closed reports and final
claims to 30 days, and action history to 45 days. These periods supersede the
original durations throughout this plan. Other cleanup and backup windows
remain as recorded in the supplied URS. Cleanup is still pending P1 work.

This is a proposed implementation plan and risk review, not an implementation
or release approval. Existing commits remain intact. The reviewed P1 commit
was local and had not been merged into main or pushed at review time. An independent code
review needs a source snapshot of this exact commit and the original URS;
the remote main branch alone is not the reviewed baseline.

The architecture can be retained. Significant changes are needed at the
notification transaction boundary, physical handover confirmation boundary,
data expiry boundary and deployment recovery boundary. Minor style, naming
and optional feature improvements are excluded from the risk findings.

## Current implementation and evidence

- Django and DRF provide HTML views and `/api/v1/` endpoints through shared
  business services. Django templates remain in use; a separate frontend
  has not yet replaced them.
- Django User, UserProfile, Report, Claim, Notice, ClaimAttachment and
  ActionHistory exist. Notice is the current notification entity.
- Report-first transaction locking and a conditional unique constraint protect
  one reservation across awaiting handover, dispute and handover in progress.
- Evidence downloads require claimant or service-staff authorization.
  Administrator status alone does not grant business authorization.
- Local migrations through 0005 are applied. Claim.report is required, but
  Claim.claimant remains nullable despite Issue 1 requiring a user relation.
- The recorded 2026-11-09 suite result was 201 passes on SQLite. This does not
  certify full URS compliance: some acceptance scenarios are missing, and
  two notification tests assert behavior contrary to A5.
- This review reran the two existing notification failure tests below:
  2 passed in 1.41 seconds. Passing here reproduces the unwanted rollback;
  it is evidence of the defect, not evidence that the requirement passes.
- No application code, migration or business data was changed for this review.
  No production, load or restore test was performed.

## Significant risk findings

| ID | Priority and status | Trigger and consequence | Required response |
| --- | --- | --- | --- |
| R1 | P0, confirmed defect | Notice creation raises during completion. The completed state, report closure and related invalidations roll back, even though the physical item may already have been handed over. A5 is not met. | Persist business results and notification intent atomically; deliver notices separately with durable retry and deduplication. Correct the contrary tests. |
| R2 | P0, confirmed control gap | The API accepts action and reason without an explicit record of the required custody or transfer confirmations. Any nonempty interruption reason currently permits a return to awaiting handover, after which withdrawal can release the reservation. The required physical conditions are not enforced as recorded attestations. | Require action-specific staff confirmations, record actor and server time, and keep uncertain handovers reserved. Human checks remain necessary; software cannot prove custody. |
| R3 | P1, confirmed design conflict | No expiry process exists. Protected audit references prevent deleting 30-day business records while retaining 45-day history. A naive cleanup could erase history or active evidence; retaining proof text inside claims or snapshots could defeat the 15-day evidence limit. | Approve an explicit retention model and restricted cleanup path before destructive migrations. Cover text, files and derived copies, not only attachment rows. |
| R4 | P2 release gate, unverified operational controls | A restored database can predate a physical handover. Resuming collection without custody reconciliation could authorize a second release. A full disk or unsuitable deployment configuration can also interrupt persistence or expose private data. No verified deployment and recovery package is present. | Pause collection during recovery, reconcile records with custody, verify backups and target-database behavior, enforce storage quotas and validate production access controls before real operation. |

R2 describes missing controls and a plausible failure path, not a claim that
an incorrect physical handover has already occurred. R4 is conditional on
deployment or recovery; it is not an observed local data loss or privacy breach.

Evidence locations at the baseline commit:

- R1: `apps/reports/models.py:239` and `:277`; report reviews also create
  notices inside their transaction at `:123`.
- R1 reproduction: `tests/test_security.py:212` and `:281` deliberately inject
  notice failures and assert rollback of the business outcome.
- R2: `apps/reports/serializers.py:42`; `apps/reports/models.py:309` and `:328`;
  `templates/staff/actions.html` contains action buttons and a reason input.
- R3: `apps/reports/models.py:391` and `:411` block audit modification/deletion;
  protected report/claim references are at `:418` and `:419`.
- R4: `config/settings.py:13`, `:14` and `:65`; deployment and performance
  limits are also recorded in `docs/p1-validation.md`.

An additional mandatory acceptance gap belongs in P1: dispute evidence cannot
currently be submitted while keeping the dispute state. See
`apps/reports/permissions.py:72` and `apps/reports/services.py:171`.
The A2-labelled test at `tests/test_workflow.py:100` enters dispute but does
not actually submit evidence in that state. Test names and pass counts must
not substitute for requirement-level acceptance evidence.

## Proposed architecture and ownership

Keep one Django application and database as the authority for business state.
HTML and API adapters use the same authorization and transition services.
Keep the existing reservation constraint and consistent lock order.

The frontend handles forms, confirmation wording, status presentation, error
recovery and API calls. The backend validates every action again and owns
data, permissions, transitions, files, audit and notification processing.
Service staff verify identity, ownership, custody and actual transfer.
Operations and backend jointly own deployment, backup and recovery checks.

Use same-origin session cookies and CSRF protection for the first separate
frontend, with a reverse proxy if needed. A different-origin design requires
a separate reviewed authentication and browser-security configuration.

Retain Django User and the existing business entities. No replacement user
table or database reset is proposed. Document Notice as the Notification
entity unless the team requires a compatible rename migration. Make
Claim.claimant non-null only after checking and resolving existing data;
never assign an arbitrary user to an orphan claim.

## P0 business safety

1. Map A1 to A6 to observable API, database and file outcomes. Correct the
   notification rollback expectations before evaluating the implementation.
   Retain valid existing permission, reservation and concurrency tests.
2. Add a durable notification outbox. A successful transaction stores the
   business state, audit entry and minimal notification event together.
   If that transaction fails, none is reported as committed. A worker creates
   the user-visible Notice in a separate transaction. Notice creation and the
   event delivery marker commit together, using a unique event/recipient key.
   Crashes and retries must not create duplicate notices. An on-commit callback
   may wake the worker but must not be the only record of pending work.
3. Retry delivery failures independently and monitor pending-event age. Keep
   private evidence out of notification payloads. Configure and test normal
   processing against the URS notice latency target of p95 at most 10 seconds.
4. Require and record action-specific staff attestations: evidence reviewed and
   item available for approval; custody confirmed when starting/resolving a
   dispute; identity and custody checked before handover start; transfer
   confirmed before completion; item not transferred or recovered before safe
   interruption. Keep reasons where required. Record trusted actor and time
   on the server. Exact field names are a proposed design, not URS wording.
5. Record handover start successfully before physical release. After an unknown
   response, query persisted status before taking another action. Completed
   remains final. An uncertain outcome remains handover in progress with its
   reservation and no automatic expiry. Repeated requests must not produce a
   second handover event. A conflict response must allow status recovery.

P0 exit evidence: notice failure after business commit preserves completion;
worker crash/retry creates one notice; simultaneous approvals have at most
one winner; start versus withdrawal has one winner; repeated completion has
one event; missing/false confirmations are rejected; uncertain transfer does
not release the reservation. Exercise both API and retained HTML entry points.

## P1 requirements and data lifecycle

1. Support private evidence submission in dispute without changing its state
   or reservation. Retain normal awaiting-evidence to pending behavior and
   attachment limits. Record dispute outcomes and withdrawal-related reasons.
2. Complete the basic-model acceptance for Issue 1, report date/time handling,
   category administration and a clearly defined correction workflow. Confirm
   with the team whether rejection with correction instructions is the accepted
   correction workflow; a new status is not automatically required by the URS.
3. Add reliable closure/final-outcome timestamps for expiry calculations.
   Do not use a mutable last-updated timestamp as the expiry origin or invent
   historical finalization dates during migration.
4. Design history to retain minimal event facts and stable object references
   after shorter-lived business records expire. Normal users and business
   operations still cannot alter history. A restricted maintenance operation
   can detach expired object references and remove expired history under the
   approved policy. Do not solve the mismatch by cascading away history.
   Include protected Notice and NotificationEvent references in this design.
5. Apply the updated Section 6.8 retention: closed reports and final claims
   30 days, private evidence 15 days after final outcome, history 45 days.
   Keep evidence needed by active cases. Remove expired live data within 7 days; protected
   backup copies expire within 14 days. Confirm campus policy before operation.
   The proposed clock for business records starts at closure/finalization;
   the reviewer must confirm this interpretation with the requirement owner.
6. Cover evidence text as well as files. Review reason fields, old file versions,
   notifications and new outbox payloads so they do not silently preserve raw
   proof longer than allowed. History should retain the decision and necessary
   actor identity, without copying full proof or unnecessary contact details.
7. Use a dry-run report and bounded, repeatable cleanup batches. Recheck active
   cases under locks. Database and filesystem deletion need retryable tracking;
   a failed file removal must remain pending. Verify backups before destructive
   migrations and demonstrate that interrupted cleanup resumes safely.
8. Track provisioned attachment capacity, warn staff at 80%, and reject writes
   that would exceed it. Account for concurrent uploads and failed writes;
   preserve previously accepted files. Reconcile usage with stored files.

P1 exit evidence: actual disputed evidence upload preserves dispute/reservation;
required user relation is enforced in the database; fresh and upgrade migrations
succeed; evidence expires after 15 days without deleting active-case evidence;
30-day deletion does not erase retained 45-day history; interrupted cleanup
and quota races are safe.

## P2 deployment and acceptance

1. Select the deployment database and run the same migration, constraint and
   concurrency checks there. SQLite test results do not certify another engine.
   PostgreSQL is an option, not a requirement stated by the URS.
2. Lock dependencies and provide a production configuration with a strong
   secret, DEBUG disabled, HTTPS and secure cookies. Private media must never
   be directly served by the proxy; report-photo visibility must remain enforced.
3. Verify daily full and hourly incremental backups, protected separately and
   retained 14 days. Restore reports, evidence and handover records. Demonstrate
   RPO at most 1 hour and RTO at most 4 hours. Keep collection paused after
   restoration until authorized staff reconcile physical custody. The pause
   must survive a restore and block release through every application route.
4. Validate all capacity tiers: 5,000/10,000/20,000 reports and
   10,000/20,000/40,000 claims; 50/100/200 active users at sustained
   10/20/40 requests per second. Attachment allowances are 100/200/400 GB,
   excluding backups; fixtures use one 2 MB image per report and two per claim.
   Use 5 minutes of warm-up and a 15-minute run,
   with the URS 70/20/10 request mix. Targets: search/detail p95 at most
   2 seconds, saved actions at most 3 seconds, notices at most 10 seconds,
   unexpected server errors below 1 percent. These targets are unverified.
5. Provide one-minute service checks and operational procedures for at least
   99 percent monthly availability, unplanned outages at most 4 hours,
   planned maintenance at most 2 hours per month and 24-hour advance notice.
6. Demonstrate A1 to A6 with the assigned client team and record the results.
   Required security, capacity and recovery checks remain release gates even
   though they are scheduled after the core implementation.

Matching improvements remain P3. First-release scope does not require SSO,
email or SMS. Payments, delivery, off-campus trading and post-completion
disputes remain outside scope. Required controls are not optional P3 work.

## Change sequence and review decisions

Preserve the current baseline and implement P0 on a new local branch. Keep
changes reviewable by concern, with migrations and relevant tests alongside
each change. Continue with P1 and then P2 gates. No reset, history rewrite,
merge, push or production action is part of this review.

The independent reviewer should decide:

1. Does the proposed outbox boundary preserve committed business results and
   provide durable, deduplicated notice delivery under crashes and retries?
2. Do the recorded confirmations and operator procedure satisfy custody and
   transfer rules without pretending to prove physical facts automatically?
3. Can the retention design meet all three durations without destroying audit
   history, retaining raw proof in copies or deleting active-case evidence?
4. Is recovery pause enforced independently of restored stale business state,
   and is physical reconciliation sufficient before collection resumes?
5. Do the acceptance tests execute every required scenario, and are the final
   deployment environment and operational targets actually verified?

Return blocking findings with the affected requirement, baseline code location
or proposed design step, failure scenario and a minimal corrective action.
Record disputed requirement interpretations separately from implementation
defects. Issue 1 completion must not be treated as whole-backend acceptance.
