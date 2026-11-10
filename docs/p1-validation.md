# P1 validation

Date: 2026-11-09. Branch: `fix/p1-backend`.
Base: `6a3d847d21f3e1c4f1b6614bea2ab23e0da22f6f`.
At this validation checkpoint, changes were local and had not been merged
or pushed. Git history records subsequent publication.

## Completed

- Validate actual JPEG/PNG content, MIME type, extension, size and decoding.
  Keep existing report photos editable and enforce claim attachment limits.
  Roll back newly stored files after failed writes without deleting old files.
- Return corrected rejected reports to review. Re-review approved reports only
  when public content changes. Block edits and transitions that conflict with
  reservations, handovers or closed reports.
- Require login for notices, service permission for match generation, and
  POST with CSRF protection for logout and notice updates. Keep redirects local.
- Share business services and allowed actions across HTML and JSON endpoints.
  Add `/api/v1/` with session authentication, private evidence, pagination,
  input validation and consistent errors. See [the API contract](api.md).
- Protect audit history against normal ORM updates, deletes and related-object
  deletion. Migration `0005_protect_action_history` preserves existing history.
- Cap regenerated match scores at 1.0, refresh existing suggestions and reuse
  staff action controls. Changed files use ASCII and spaces without tabs.

## Verification

Environment: Windows, Python 3.13.9, Django 6.1.3, DRF 3.18.3,
Pillow 13.0.0, pytest 9.2.0 and SQLite.
Commands were run from the repository with `.venv/Scripts/python.exe`.

| Check | Result |
| --- | --- |
| `-m pytest -q --tb=short` | 201 passed in 72.95 seconds |
| `-m ruff check . --output-format concise` | Passed |
| `manage.py check` | No issues |
| `manage.py makemigrations --check --dry-run` | No changes detected |
| `manage.py migrate --noinput` | Local database migrated through 0005 |
| `manage.py migrate --check` | No unapplied migrations |
| `git diff --check` | Passed |
| Changed-file ASCII and tab scan | Passed |
| `manage.py check --deploy` | Six development configuration warnings |

The suite includes permission checks, upload validation and rollback,
report/claim/dispute/handover workflows, concurrent reservation races,
audit protection, migrations, HTML rendering, and API contracts.
A real loopback HTTP test covers CSRF rejection, session login, report
creation, private report visibility, logout and subsequent authentication.

The first sandboxed run had 200 passes and one loopback socket permission
failure (Windows error 10013). The complete suite passed after allowing
local loopback access. No external service was used.

## Remaining work and limits

1. The standalone frontend still needs implementation and API integration.
   Existing Django templates remain operational. The API currently assumes
   same-origin session cookies; a same-origin proxy can serve a separate app.
2. Production configuration remains pending. Deployment checks report W004,
   W008, W009, W012, W016 and W018: HSTS, HTTPS redirects, the development
   secret key, secure session/CSRF cookies and DEBUG. The local development
   configuration is not a verified production configuration.
3. PostgreSQL is not configured or tested. Concurrency tests passed on SQLite;
   rerun migration and concurrency checks against the deployment database.
4. Dependencies remain open-ended and `uv.lock` is ignored. Agree and commit
   a dependency lock before relying on reproducible deployment builds.
5. Large-data performance remains unmeasured. HTML lists need pagination,
   and match generation still compares all eligible lost/found pairs.
   Existing match scores are refreshed when staff regenerate suggestions.
6. Audit protection is enforced through application ORM paths. Raw SQL and
   database administrator access require separate operational controls.

No browser visual review, load test, production deployment or external
frontend integration test was performed.

## Review with URS P0 on 2026-11-10

Retain the P1 implementation in `ab3724a`. URS P0 in `ad31a8e` depends on its
shared services, API, staff action controls and migration 0005. P0 corrects
notification failure handling and requires explicit handover confirmations.
The combined implementation passed 465 tests; see [P0 validation](urs-p0-validation.md).
This publication review changes documentation only and does not rerun that suite.

The earlier P1 label does not mean all P1 work identified by the later URS
review is complete. Disputed evidence submission, remaining decision reasons,
Issue 1 model requirements, retention and storage quotas remain tracked work.

The root `api.md` added on remote main at `bd464f6` describes `/api` routes,
different input names and response envelopes. This branch implements the
`/api/v1/` contract in `docs/api.md`. These contracts are not interchangeable.
Agree the frontend contract and update documentation and integration tests
before merging; pushing this development branch does not resolve that choice.

Publish P1 and P0 together to `fix/urs-p0` for review. Keep main unchanged.
This is not full URS acceptance or approval for production deployment.
