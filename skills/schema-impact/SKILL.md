---
name: schema-impact
description: Report breaking schema drift and downstream impact. Use for schema_drift incidents. Never propose golden writes.
---

# Schema impact

Procedure:

1. `load_contract` for the expected schema.
2. `diff_schemas` against the breaking fixture.
3. `lookup_lineage` for `SCHEMA-CUSTOMER`.
4. `downstream_impact` for the affected fields.
5. Outcome is always **report_only**. Leave `changes` empty. Do not propose golden-record edits or migrations.

A human Acknowledge closes the ticket. The agent does not ALTER or write.
After any context compaction, call `load_skill` with `schema-impact` again.
