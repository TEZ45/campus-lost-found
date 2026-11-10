API Specification (Draft v1.0)
 
This document defines the core RESTful API interfaces for the Campus Lost and Found System. It serves as the contract between the Frontend and Backend teams.
 
Base URL: /api
Data Format: JSON
Authentication: JWT Token (to be added in the Authorization header) 
 
1. Reports
 
1.1 Submit a Report (REQ-1.2)
• Method: POST
• Endpoint: /api/reports
• Description: Accepts lost/found details. Only approved content is public.
• Request Body (Multipart/Form-Data):
	• type: String (Required, enum: "lost", "found")
	• item_name: String (Required)
	• category: String (Required)
	• description: String
	• date_time: String (Required, ISO 8601 format)
	• location: String (Required)
	• contact: String (Required)
	• photo: File (Optional, JPG/PNG, Max 5MB)
• Response (201 Created):
{
  "code": 201,
  "message": "Report submitted successfully. Pending review.",
  "data": { "report_id": 101, "status": "Pending Review" }
}
 
1.2 Search Reports (REQ-1.5)
• Method: GET
• Endpoint: /api/reports/search
• Query Parameters: keyword, type, category, location, date, status
• Response (200 OK):
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
 
 
2. Claims
 
2.1 Submit a Claim (REQ-1.7, REQ-1.8)
• Method: POST
• Endpoint: /api/claims
• Description: Accepts private evidence for an open found-item report.
• Request Body (Multipart/Form-Data):
	• report_id: Integer (Required)
	• evidence_text: String (Required, Private identifying details)
	• evidence_images: Files (Optional, Up to 3 images, Max 5MB each)
• Response (201 Created):
{
  "code": 201,
  "message": "Claim submitted. Pending review.",
  "data": { "claim_id": 501, "status": "Pending Review" }
}
 
2.2 Staff Review Claim (REQ-1.8, REQ-1.9)
• Method: PUT
• Endpoint: /api/claims/{claim_id}/status
• Description: Staff action to approve, reject, or request evidence. Note: Approval requires exclusive reservation logic (REQ-1.9).
• Request Body:
	• action: String (Required, enum: "approve", "reject", "request_evidence")
	• reason: String (Required if action is "reject" or "request_evidence")
• Response (200 OK):
{
  "code": 200,
  "message": "Claim status updated.",
  "data": { "claim_id": 501, "new_status": "Awaiting Handover" }
}
 
 
3. Handover (Staff only)
 
3.1 Start Handover (REQ-1.11)
• Method: POST
• Endpoint: /api/handovers/start
• Request Body: claim_id: Integer (Required)
• Response (200 OK): Status changes to "Handover in Progress". 
3.2 Complete Handover (REQ-1.11, REQ-1.12)
• Method: POST
• Endpoint: /api/handovers/complete
• Request Body: claim_id: Integer (Required)
• Response (200 OK):
	• Status changes to "Completed".
	• The found-item report is automatically closed.
	• Other claims for this item are invalidated. 
 
Note: Detailed error handling (Section 6.9) and state transitions (Figure 2) must adhere strictly to the URS document.
