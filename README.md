# Campus Lost and Found Matching System

A software engineering course project built with Python and Django.
The system helps campus users publish lost and found reports,
find possible matches, and submit claims for staff review.

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

## Project Structure

```
campus-lost-found/
├── config/              # Django project settings
│   ├── settings.py
│   ├── urls.py
│   └── wsgi.py
├── apps/
│   └── reports/        # Reports, claims, and matches
│       ├── models.py
│       ├── views.py
│       ├── admin.py
│       └── forms.py
├── templates/           # HTML templates
├── static/              # Static files (CSS, JS, images)
├── docs/                # Documentation
├── tests/               # Test files
├── manage.py
└── pyproject.toml
```

## Features

- Submit lost and found reports (with photos)
- Staff review and approve reports in admin
- Browse approved reports by type (lost / found)
- Submit private claim evidence for found items
- Staff review claims and approve / reject
- Match suggestions between lost and found items

## Tech Stack

- Backend: Django 5 + Django REST Framework
- Frontend: HTML templates with vanilla CSS/JS
- Database: SQLite (dev) / PostgreSQL (prod)
- Package manager: uv

## Team Collaboration

All four members contribute to development, testing, and documentation.
Feature changes use separate branches and pull requests for review.
