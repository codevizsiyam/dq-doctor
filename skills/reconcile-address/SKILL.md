---
name: reconcile-address
description: Reconcile conflicting customer addresses into a golden-record proposal. Use for address_discrepancy incidents.
---

# Reconcile address

Procedure:

1. `lookup_customer` for the incident `customer_id`.
2. `compare_records` to list conflicting fields.
3. `validate_address` on each operational source (CRM, Billing, Support).
4. `load_contract` and follow `source_authority` for the field (Billing first for street/city/state/zip).
5. Cite evidence as `source:record_id:field`.
6. Outcome:
   - **recommend** a golden update when one authoritative valid value wins.
   - **escalate** when three (or more) distinct valid streets disagree. Do not pick a winner.

Never invent records. Never call a write tool. Confidence does not authorize a write.
After any context compaction, call `load_skill` with `reconcile-address` again.
