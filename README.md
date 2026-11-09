# Campus Lost and Found Matching System

A software engineering course project built with Python and Django.
The system helps campus users publish lost and found reports,
find possible matches, and submit claims for staff review.

Implementation follows the *Campus Lost and Found System* User Requirements
Specification (URS): accounts & roles, report moderation, search, private
claims with an exclusive reservation, dispute review, physical handover,
in-app notices and immutable action history. See
[the URS coverage matrix](docs/URS_%E9%9C%80%E6%B1%82%E8%A6%86%E7%9B%96%E7%9F%A9%E9%98%B5.md)
for the requirement-by-requirement trace.

## Quick Start

### Prerequisites

- Python 3.12+
- uv (recommended) or pip

### Setup

```bash
# Install dependencies
uv sync

# Run migrations
uv run python manage.py migrate

# Create superuser (for admin)
uv run python manage.py createsuperuser

# Start dev server
uv run python manage.py runserver
```

Then open http://127.0.0.1:8000/

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
uv run python manage.py migrate
uv run python manage.py move_private_evidence
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

```bash
uv run --extra dev python -m pytest
```

The suite covers URS acceptance checks A1-A6 (exclusive reservation,
evidence access control, handover/withdrawal races, safe interruption,
completion invalidation) plus search, roles, notices and attachment limits.
Tests use temporary media directories and a temporary SQLite file. Concurrent
workflow tests use separate database connections. SQLite write transactions
use `IMMEDIATE` mode; PostgreSQL deployment still needs separate validation.

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
- In-app notices for users and immutable action history for staff
- Suggested lost/found matches with reasons (never auto-approve claims)

## Tech Stack

- Backend: Django; Django REST Framework installed for future API work
- Frontend: HTML templates with vanilla CSS/JS
- Database: SQLite; PostgreSQL deployment is not configured yet
- Package manager: uv

## Team Collaboration

All four members contribute to development, testing, and documentation.
Feature changes use separate branches and local merges after review.

