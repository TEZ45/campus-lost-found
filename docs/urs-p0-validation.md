# URS P0 implementation and validation

Date: 2026-11-10. Local branch: `fix/urs-p0`.
Base commit: `ab3724a1049c823757367f653f4a3e9908af426b`.
At validation time, the P0 changes were uncommitted and had not been merged
or pushed. Git history records subsequent publication.

## Implemented

- Business state, audit history and notification events commit together.
  Notice delivery runs separately. A failed delivery preserves the saved
  report/claim result and remains retryable.
- Notice creation and its delivery marker commit atomically. A unique event
  relation prevents duplicate notices; concurrent workers and a failure before
  the marker is saved are covered by tests.
- The worker processes bounded batches, favors events with fewer failed
  attempts, and reports failures, pending count and oldest pending age.
  Errors store only an exception class, without raw proof or private messages.
- Approval, disputes and handover require explicit action-specific staff
  confirmations. The model checks them after authorization and locked-state
  validation, before writes. API and HTML adapters share those rules.
- Audit history records confirmations with the server-assigned actor and time.
  Missing, false, unknown and incorrectly typed confirmations cannot alter
  reports, claims, history or queued notices. HTML controls are not preselected.
- Safe interruption requires a verified not-transferred or recovered outcome.
  Uncertain handovers retain their state and reservation. Repeated completion
  is rejected without another handover record; clients can query saved status.
- Migration 0006 adds the outbox, optional Notice event relation and audit
  confirmation field. Existing notices and history are preserved without
  fabricating historical confirmations. It has been applied locally.

## Verification

| Check | Result |
| --- | --- |
| Full suite, including real loopback HTTP | 465 passed in 103.75 seconds |
| New confirmation tests | 238 passed in the focused run |
| New notification tests | 24 passed in the focused run |
| Ruff | Passed |
| Django system checks | No issues |
| Migration consistency | No changes detected |
| Local migration status | No unapplied migrations |
| Git diff whitespace check | Passed |
| Changed-file character scan | ASCII only, no tabs |

The full run used `.venv/Scripts/python.exe -m pytest -q --tb=short` with
permission to access temporary loopback HTTP servers. An earlier sandboxed
HTTP attempt failed with Windows socket error 10013 before reaching the API;
both HTTP tests and the later full suite passed with loopback access enabled.

The previous tests that expected business rollback on notification failure
were corrected to assert the URS-required saved outcome. Existing valid
workflow tests now provide explicit staff confirmations rather than bypassing
the new validation. Tests use temporary databases and media directories.

## Acceptance boundaries

| URS check | Current evidence |
| --- | --- |
| A1 | Concurrent approvals and reservation conflicts covered |
| A2 | Ordinary evidence resubmission and evidence privacy covered; evidence submission during dispute remains P1 work |
| A3 | Handover versus withdrawal/cancellation races covered |
| A4 | Confirmation requirements, safe interruption, state queries and repeated completion covered |
| A5 | Related invalidation, final outcomes and independent notice failure/retry covered |
| A6 | Existing report, combined-search and ownership checks retained |

This is P0 verification, not a claim that every URS requirement is complete.
Human identity, ownership, custody and transfer checks remain staff duties.

## Running notifications

Run alongside the web application:

```text
.venv/Scripts/python.exe manage.py deliver_notifications
```

The default poll interval is one second with at most 100 events per batch.
Use `--once` for a single batch. Deployment must supervise the worker and
monitor pending age; otherwise notifications remain queued while saved
business status is still queryable. The URS latency target has not been
certified under production capacity-tier load.

## Remaining work

- P1: disputed evidence submission, remaining reason rules, Issue 1 schema
  acceptance, date/time and category requirements, retention and total quota.
- P2: production configuration, target-database and load verification,
  backup/restore drills and a recovery pause before physical reconciliation.
- The earlier backend plan review remains a historical review of the base
  commit. Its P0 findings are addressed by these changes; its P1/P2 items remain.
- GitHub Issue 1 was not changed. Browser operations ended because the tool
  could not reliably verify the current Windows browser URL. No usable GitHub
  API credential was available. The user-approved text remains saved locally
  at `E:/SE/urs-review/issue-1-approved.md` for a later authenticated update.

Validation did not deploy to production, merge, push or close the Issue.
