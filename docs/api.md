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
`location`, `event_date`, `contact_name`, `contact_email`, `contact_phone`, and
`photo`. Dates use `YYYY-MM-DD`. PUT sends all required report fields; PATCH
keeps omitted fields. `photo: null` clears the photo. Send image uploads using
multipart form data. Claim writes accept `evidence` and repeated `images`
parts, up to three images per request and five per claim.

Review requests accept `action` and optional `reason`. Report actions are
`approve`, `reject`, `close`. Staff claim actions are `approve`, `reject`,
`request_evidence`, `start_dispute`, `resolve_keep`, `resolve_reject`,
`start_handover`, `complete_handover`, `interrupt_handover`. Rejection and safe
interruption require a reason. Consult each resource's `allowed_actions`
array of `{key, label, reason}` before rendering controls. The server checks
the action again inside the transaction.

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
