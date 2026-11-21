# Campus Lost and Found Matching System

A software engineering course project built with Python and Django.
The system helps campus users publish lost and found reports,
find possible matches, and submit claims for staff review.

Implementation follows the *Campus Lost and Found System* User Requirements
Specification (URS): accounts & roles, report moderation, search, private
claims with an exclusive reservation, dispute review, physical handover,
in-app notices and append-only action history. See
[the URS coverage matrix](docs/URS_%E9%9C%80%E6%B1%82%E8%A6%86%E7%9B%96%E7%9F%A9%E9%98%B5.md)
for the requirement-by-requirement trace.

## Quick Start

### Prerequisites

- Python 3.13 (tested; minimum 3.12)
- uv 0.13.9

### Setup

```bash
# Install dependencies
uv sync --frozen --extra dev

# Run migrations
uv run --frozen --extra dev python manage.py migrate

# Create superuser (for admin)
uv run --frozen --extra dev python manage.py createsuperuser

# Start dev server
uv run --frozen --extra dev python manage.py runserver
```

Then open http://127.0.0.1:8000/

Local development defaults to SQLite. PostgreSQL 17 is the deployment target;
see [runtime setup](docs/runtime.md) for environment profiles, separate database
roles and isolated integration tests. Production and test profiles ignore `.env`.

Run the notification worker in a second terminal:

```bash
uv run --frozen --extra dev python manage.py deliver_notifications
```

The worker polls every second. Business changes persist a notification event
in the same transaction; notice delivery happens separately and can be retried.
Without the worker, events remain queued and users can still query saved status.
Use `--once` for a bounded batch, `--limit` for batch size and `--interval` for
continuous polling. JSON output reports delivered, failed and pending events
and the oldest pending age. Monitor this age against the 10-second notice target.
Run the worker under a process supervisor in deployment.

### Assigning staff (SEC-1.3)

System administrator status alone does NOT grant lost-property operations.
In the Django admin, open a user and tick **Service authorisation** >
`is service staff` to authorise moderation, claim review and handover work.

Business records in Django admin are read-only and require service
authorisation. Use `/staff/` for workflow changes. Deactivate accounts
instead of deleting them and their related claims.

### Updating evidence storage

Stop the application before updating an existing installation, then run:

```bash
uv run --frozen --extra dev python manage.py migrate
uv run --frozen --extra dev python manage.py move_private_evidence
```

Restart after both commands succeed. The migration refuses duplicate active
reservations; resolve those records before retrying. The file move preserves
database paths, is repeatable, and refuses to overwrite conflicting files.

Evidence is stored in `private_media/` and downloaded through an authenticated
claim attachment endpoint. Never expose that directory through a web server.
Route `/media/` through Django as well: report photos follow report visibility,
and legacy claim URLs are blocked. Existing evidence remains available through
the authenticated endpoint during the file move.

### Tests

Set APP_ENV=test in the process environment and select an isolated database
before running the suite. See [runtime setup](docs/runtime.md) for PostgreSQL.

```bash
uv run --frozen --extra dev python -m pytest
```

The suite includes checks corresponding to URS A1-A6 (exclusive reservation,
evidence access control, handover/withdrawal races, safe interruption,
completion invalidation) plus search, roles, notices and attachment limits.
Tests use temporary media and an isolated database. Concurrent workflows use
separate connections; SQLite uses `IMMEDIATE` transactions and PostgreSQL uses
row locks. Real Redis tests require REDIS_TEST_URL and otherwise skip explicitly.
Set TEST_REQUEST_LIMITS_BACKEND=redis to exercise ordinary workflow requests
against Redis as well. See [Redis validation](docs/redis-validation.md) for
the separate fault-test service and the database selection review.
See [the P2 validation record](docs/p2-runtime-review.md) for executed checks and
remaining deployment work. Passing automated tests does not establish complete
URS acceptance. See [the schema](docs/schema.md) and
[the P1 review](docs/p1-abuse-review.md) for migrations, limits and maintenance.

### API and business rules

The JSON API is available at `/api/v1/`; see [the API contract](docs/api.md)
for routes, fields, permissions, errors and session/CSRF handling.
`services.py` handles writes for both HTML views and API views. Models retain
the transaction-safe state transitions. Shared action definitions drive UI
controls and server checks. A separate frontend can use the API; the existing
Django templates remain available during that transition.

Claim approval, disputes and handover actions require explicit staff
confirmations, recorded in audit history. Confirmations must be submitted by
the operator; they are never inferred from a button click or filled in by the
server. After an unknown response, query current status before acting again.
An uncertain handover remains in progress and retains its reservation.

Audit records reject application-level updates and deletes. Related users,
reports and claims cannot be deleted while referenced by audit history.
Database administrator access and raw SQL are outside these application rules.

## Project Structure

```
campus-lost-found/
  config/               # Django settings and routes
  apps/reports/         # Reports, claims, matches, notices, history
  templates/            # User pages and staff queues
  media/                # Report photos
  private_media/        # Private claim evidence
  docs/                 # URS coverage matrix
  tests/                # pytest suite
  manage.py
  pyproject.toml
```

## Features

- Accounts with campus user / service-staff roles (register, login)
- Submit lost and found reports with category and photo (CAP-3.1 limits)
- Staff moderation queue: approve / reject with reason / closure requests
- Owner editing of own reports; changed public content needs re-review
- Combined search: keyword, type, category, location, date, status
- Private claim evidence; only claimant and authorised staff may read it
- Claim state model: pending -> evidence -> approval -> handover -> completed,
  with dispute review, withdrawal, invalidation and exclusive reservation
- Physical handover recording (start / complete / safe interruption)
- In-app notices for users and append-only action history for staff
- Suggested lost/found matches with reasons (never auto-approve claims)

## Tech Stack

- Backend: Django and Django REST Framework
- Frontend: HTML templates with vanilla CSS/JS
- Database: PostgreSQL 17 target; SQLite for local development
- Package manager: uv

## Team Collaboration

All four members contribute to development, testing, and documentation.
Feature changes use separate branches. After local validation and authorization
to publish, push the feature branch and open a GitHub pull request for team
review. Merge on GitHub after review and checks; synchronize local main with
`git pull --ff-only`. Do not merge feature branches into main locally.

