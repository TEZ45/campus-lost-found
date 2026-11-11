# API reference

The implemented backend uses `/api/v1/`. Use [the current contract](docs/api.md)
for routes, fields, response formats, permissions and session/CSRF handling.

The `/api` routes and payloads below belong to the original proposal and are
not exposed by the current backend. Handover actions require explicit staff
confirmations. Completion invalidates only other claims before handover;
previous final outcomes remain unchanged. Follow the current contract when
building or updating a frontend client.

## Original API proposal

Retained for design history. These examples do not describe the implemented API.

API Specification (Campus Lost and Found)
This document defines the core interfaces between Frontend and Backend.
Base URL: /api
Data Format: JSON

1. Reports
1.1 Submit a Report (REQ-1.2)
Method: POST

Endpoint: /api/reports

Request Body:

type: String (required: lost/found)

item_name: String

category: String

location: String

Response (201 Created)
```json
 {
  "code": 201,
  "message": "Report submitted successfully",
  "data": {
    "report_id": 101,
    "status": "Pending Review"
  }
}
```

1.2 Search Reports (REQ-1.5)
- Method: GET
- Endpoint: /api/reports/search
- Query Parameters: keyword, type, category, location, date, status
- Response (200 OK):
```json
{
  "code": 200,
  "data": [
    {
      "report_id": 101,
      "item_name": "Black Wallet",
      "category": "Accessories",
      "location": "Library",
      "date_time": "2026-10-20T14:30:00Z",
      "photo_url": "/uploads/img_101.jpg",
      "status": "Approved"
    }
  ]
}
```


2. Claims

2.1 Submit a Claim (REQ-1.7, REQ-1.8)
- Method: POST
- Endpoint: /api/claims
- Description: Accepts private evidence for an open found-item report.
- Request Body (Multipart/Form-Data):
    - report_id: Integer (Required)
    - evidence_text: String (Required, Private identifying details)
    - evidence_images: Files (Optional, Up to 3 images, Max 5MB each)
- Response (201 Created):
```json
{
  "code": 201,
  "message": "Claim submitted. Pending review.",
  "data": { "claim_id": 501, "status": "Pending Review" }
}
```

2.2 Staff Review Claim (REQ-1.8, REQ-1.9)
- Method: PUT
- Endpoint: /api/claims/{claim_id}/status
- Description: Staff action to approve, reject, or request evidence. Note: Approval requires exclusive reservation logic (REQ-1.9).
- Request Body:
    - action: String (Required, enum: "approve", "reject", "request_evidence")
    - reason: String (Required if action is "reject" or "request_evidence")
- Response (200 OK):
```json
{
  "code": 200,
  "message": "Claim status updated.",
  "data": { "claim_id": 501, "new_status": "Awaiting Handover" }
}
```


3. Handover (Staff only)

3.1 Start Handover (REQ-1.11)
- Method: POST
- Endpoint: /api/handovers/start
- Request Body: claim_id: Integer (Required)
- Response (200 OK): Status changes to "Handover in Progress".
3.2 Complete Handover (REQ-1.11, REQ-1.12)
- Method: POST
- Endpoint: /api/handovers/complete
- Request Body: claim_id: Integer (Required)
- Response (200 OK):
    - Status changes to "Completed".
    - The found-item report is automatically closed.
    - Other claims for this item are invalidated.

Note: Detailed error handling (Section 6.9) and state transitions (Figure 2) must adhere strictly to the URS document.
