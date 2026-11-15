# Backend API

Base path: `/api/v1/`. Responses are JSON except authenticated attachment
downloads. Browser clients use the Django session cookie. A separate frontend
can run behind the same-origin reverse proxy; no token or CORS dependency is
required for this arrangement.
The API is implemented alongside the existing Django templates. A separate
frontend has not yet replaced those templates.

Fetch `GET auth/csrf/` first. Send its `csrf_token` as `X-CSRFToken` on every
unsafe request, including login and registration, and include cookies.
Login and registration rotate the token and return the replacement.
CSRF failures return 403. Unauthenticated protected reads return 401.

## Routes

| Method | Path | Purpose |
| --- | --- | --- |
| GET | `auth/csrf/` | Set CSRF cookie and return token |
| GET | `auth/me/` | Current ID, username, business role |
| POST | `auth/register/` | Register with username, password1, password2 |
| POST | `auth/login/` | Login with username and password |
| POST | `auth/logout/` | End session |
| GET | `categories/` | Paginated active category suggestions |
| GET, POST | `reports/` | Search visible reports or submit one |
| GET | `reports/mine/` | Own reports, including nonpublic reports |
| GET, PUT, PATCH | `reports/{id}/` | Read or update an authorised report |
| POST | `reports/{id}/close/` | Request closure |
| POST | `reports/{id}/claims/` | Submit private ownership evidence |
| GET | `claims/` | Own claims |
| GET | `claims/{id}/` | Claimant or service-staff claim detail |
| POST | `claims/{id}/resubmit/` | Submit replacement evidence and optional images |
| POST | `claims/{id}/withdraw/` | Withdraw before handover |
| GET | `attachments/{id}/` | Download evidence after authorisation |
| GET | `notices/` | Own notices |
| POST | `notices/{id}/read/` | Mark an own notice read |
| GET | `matches/` | Public suggested matches |
| POST | `matches/regenerate/` | Service-staff regeneration |
| GET | `staff/dashboard/` | Report and claim counts by status |
| GET | `staff/reports/` | Pending reports and closure requests |
| POST | `staff/reports/{id}/review/` | Review a report |
| GET | `staff/claims/` | Pending claims and evidence requests |
| POST | `staff/claims/{id}/actions/` | Review, dispute or handover action |
| GET | `staff/handovers/` | Reserved claims |
| GET | `staff/history/` | Read-only audit history |

## Inputs and outputs

Report writes accept `report_type`, `item_name`, `category`, `description`,
`location`, `event_date`, `event_time`, `contact_name`, `contact_email`, `contact_phone`, and
`photo`. Category and event time are required on creation and PUT. Dates use
`YYYY-MM-DD`; event time uses campus-local `HH:MM[:SS]` in Asia/Shanghai.
Unknown legacy times remain null until supplied. PUT sends all required report fields; PATCH
keeps omitted fields. `photo: null` clears the photo. Send image uploads using
multipart form data. Claim writes accept `evidence` and repeated `images`
parts, up to three images per request and five per claim.

Review requests accept `action` and optional `reason`. Report actions are
`approve`, `reject`, `close`. Staff claim actions are `approve`, `reject`,
`request_evidence`, `start_dispute`, `resolve_keep`, `resolve_reject`,
`start_handover`, `complete_handover`, `interrupt_handover`. Rejection and safe
interruption, evidence requests, dispute start/outcomes and report closure
require a reason. Closure requests and claimant withdrawals accept
`{"reason": "..."}` and reject a missing or blank reason. Consult each resource's `allowed_actions`
array of `{key, label, reason, confirmations}` before rendering staff controls.
User actions may omit `confirmations`. The server checks
the action again inside the transaction.

Evidence is limited to 10000 characters, reasons to 2000, and report descriptions
to 10000. Search limits are 200 for `q`, 100 for `category` and 200 for `location`.
Images remain JPEG/PNG, at most 5 MiB each; decoded images are limited to
20 million pixels by default. New image submissions remain at most three
images per request and five per claim. Aggregate file data is limited to
15 MiB and the total body to 17 MiB. Exceeding either stream budget returns 413.

Evidence resubmission from `awaiting_evidence` returns to `pending`; from
`under_dispute` it stays disputed and retains the reservation. Claim responses
include `finalized_at`, `evidence_purged_at` and handover actor/time fields.
After evidence expiry, text is empty and attachments disappear; the expiry
marker explains this result. Report responses include `closed_at`. Audit
history has stable report/claim references even after those objects expire.
`current_reason` on private claim responses and `review_reason` for report
managers preserve current instructions while a case remains active, even
after older audit history expires. They are scrubbed with final evidence;
report reasons are never exposed to unrelated public readers.

Staff claim actions also accept a `confirmations` object. Boolean values must
be JSON `true`, supplied explicitly by the operator. Missing, false, unknown
or incorrectly typed confirmations return 400 without changing the claim.
Report review actions do not accept this field.

| Claim action | Required confirmations |
| --- | --- |
| `approve` | `evidence_reviewed: true`, `custody_confirmed: true` |
| `start_dispute`, `resolve_keep`, `resolve_reject` | `custody_confirmed: true` |
| `start_handover` | `identity_verified: true`, `custody_confirmed: true` |
| `complete_handover` | `transfer_confirmed: true` |
| `interrupt_handover` | `safe_outcome: "not_transferred"` or `"recovered"` |

Each staff action lists its required confirmation inputs as `{key, label}`,
with optional `choices: [{value, label}]`. No confirmation is selected by
default. History includes the accepted confirmations with the server-recorded
actor and time. These are staff attestations, not automatic proof of custody.

Record handover start successfully before releasing the item. After an unknown
response, GET the claim and review its status/history before another action.
Completed remains final; uncertain physical outcomes remain in progress and
retain their reservation. Repeating a completed transition returns 409 and
does not create another handover record.

Status changes enqueue durable notification events. Notice delivery requires
the `deliver_notifications` worker and can be retried independently. A notice
delivery failure does not roll back committed claim or report outcomes.

Unknown input fields are rejected. Clients cannot assign status, ownership,
reviewer, staff role or audit fields. Public reports omit contact data;
authorised managers additionally receive a `contact` object. Claim evidence,
attachments and claim history require claimant or service-staff access.

Lists return `{count, next, previous, results}`. Default page size is 20;
`page` and `page_size` are supported, with a maximum size of 100. Report search
accepts `q`, `report_type`, `category`, `location`, `date_from`, `date_to`, and
`status`. Status filtering cannot bypass report visibility.

Successful creation returns 201, other reads/writes 200, and logout 204.
Errors use `{error: {code, details}}`: invalid input 400, authentication 401,
permission or CSRF 403, missing/inaccessible report 404, method 405, and state
conflict 409. Refresh a resource after a conflict before retrying. Evidence
downloads use an attachment disposition and private, no-store caching.

## Request and work budgets

API and HTML routes enforce shared IP and authenticated-account windows,
including separate login, registration, search, write and upload budgets.
Rate rejection returns 429 with `Retry-After`; unavailable limit accounting
returns 503 rather than silently accepting work. Wait before retrying and
query saved status before repeating a business action.

Matching regeneration has a shared lease, a default 30-second cooldown,
2500-pair cap and a 10-second cooperative processing budget. Busy/deadline
rejections return 429; too many pairs returns 400. These caps do not replace
database statement timeouts or upstream DDoS protection.

Staff dashboard responses include storage capacity, accounted usage,
initialization state and an 80-percent warning. Uploads exceeding the
configured capacity are rejected before permanent storage writes.
