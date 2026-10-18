# Campus Lost and Found Matching System

A software engineering course project built with Python and Django.
The system helps campus users publish lost and found reports,
find possible matches, and submit claims for staff review.

Implementation follows the *Campus Lost and Found System* User Requirements
Specification (URS): accounts & roles, report moderation, search, private
claims with an exclusive reservation, dispute review, physical handover,
in-app notices and immutable action history. See
`docs/URS_需求覆盖矩阵.md` for the requirement-by-requirement trace.

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

### Tests

```bash
uv run python -m pytest
```

The suite covers URS acceptance checks A1-A6 (exclusive reservation,
evidence access control, handover/withdrawal races, safe interruption,
completion invalidation) plus search, roles, notices and attachment limits.

## Project Structure

```
campus-lost-found/
├── config/              # Django project settings
│   ├── settings.py
│   ├── urls.py
│   └── wsgi.py
├── apps/
│   └── reports/        # Reports, claims, matches, notices, history
│       ├── models.py
│       ├── views.py
│       ├── admin.py
│       └── forms.py
├── templates/           # HTML templates (user pages + staff queues)
├── static/              # Static files (CSS, JS, images)
├── docs/                # Documentation (URS trace matrix)
├── tests/               # pytest suite
├── manage.py
└── pyproject.toml
```

## Features

- Accounts with campus user / service-staff roles (register, login)
- Submit lost and found reports with category and photo (CAP-3.1 limits)
- Staff moderation queue: approve / reject with reason / closure requests
- Owner editing of own reports; changed public content needs re-review
- Combined search: keyword, type, category, location, date, status
- Private claim evidence; only claimant and authorised staff may read it
- Claim state model: pending → evidence → approval → handover → completed,
  with dispute review, withdrawal, invalidation and exclusive reservation
- Physical handover recording (start / complete / safe interruption)
- In-app notices for users and immutable action history for staff
- Suggested lost↔found matches with reasons (never auto-approve claims)

## Tech Stack

- Backend: Django 6 + Django REST Framework
- Frontend: HTML templates with vanilla CSS/JS
- Database: SQLite (dev) / PostgreSQL (prod)
- Package manager: uv

## Team Collaboration

All four members contribute to development, testing, and documentation.
Feature changes use separate branches and pull requests for review.

