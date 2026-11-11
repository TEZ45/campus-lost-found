Test Plan & Acceptance Checks

This document outlines the acceptance test cases based on Section 6.6 of the URS document.

For the Reviewer: Please execute these test cases after the backend and frontend implementations are ready, and fill in the Actual Result and Status for each item.


1. Acceptance Checks (A1 - A6)

A1. Concurrent Approval & Exclusive Reservation
Related Req.: REQ-1.9, REQ-1.10

Test Scenario: User A and User B submit claims for the same item. Staff approves both claims simultaneously.

Expected Result: Only 1 claim is approved and reserved. The other claim retains its previous status. A dispute does not allow a second approval.

Actual Result: (To be filled by Reviewer)

Status: ⬜ Pass ⬜ Fail

A2. Evidence Privacy & Resubmission
Related Req.: REQ-1.8, SEC-1.2

Test Scenario: Staff requests additional evidence. Claimant submits evidence. Unauthorized user tries to access the evidence via URL.

Expected Result: Claim status returns to Pending Review. Unauthorized user is denied access to the evidence.

Actual Result: (To be filled by Reviewer)

Status: ⬜ Pass ⬜ Fail

A3. Handover & Withdrawal Conflict
Related Req.: REQ-1.11, REQ-1.12

Test Scenario: Claim is in Awaiting Handover. Staff clicks "Start Handover" and user clicks "Withdraw" at the same time.

Expected Result: Only 1 action succeeds. Ongoing handover blocks withdrawal and report cancellation.

Actual Result: (To be filled by Reviewer)

Status: ⬜ Pass ⬜ Fail

A4. Interrupted Handover (Network loss)
Related Req.: REQ-1.11

Test Scenario: Staff clicks "Confirm Handover" but the network disconnects.

Expected Result: If completion was saved, retain Completed. If uncertain, status remains Handover in Progress with reservation. No duplicate handover records created.

Actual Result: (To be filled by Reviewer)

Status: ⬜ Pass ⬜ Fail

A5. Invalidation after Completion
Related Req.: REQ-1.12, REQ-1.14

Test Scenario: Staff completes handover for Claim A. Check other claims (Claim B) for the same item.

Expected Result: Claim A is Completed. Claim B (Before Handover) becomes Invalidated. Previous final outcomes remain unchanged.

Actual Result: (To be filled by Reviewer)

Status: ⬜ Pass ⬜ Fail

A6. Basic Logic & Access Control
Related Req.: REQ-1.1 - REQ-1.5

Test Scenario: 1. Submit invalid data. 2. User tries to modify another user's report.

Expected Result: Invalid input preserves entered data and shows error. Unauthorized modification is blocked.

Actual Result: (To be filled by Reviewer)

Status: ⬜ Pass ⬜ Fail


2. Performance Checks (P1 - P2)

P1. Search Request Latency
Related Req.: PERF-1.2

Test Scenario: Search and report-detail requests under load.

Expected Result: 95% of requests finish within 2 seconds.

Actual Result: (To be filled by Reviewer)

Status: ⬜ Pass ⬜ Fail

P2. Claim Submission Latency
Related Req.: PERF-1.3

Test Scenario: Saved claim submission and review actions under load.

Expected Result: 95% of requests finish within 3 seconds.

Actual Result: (To be filled by Reviewer)

Status: ⬜ Pass ⬜ Fai
