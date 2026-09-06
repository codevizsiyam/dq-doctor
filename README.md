# Data Steward AI

Governed investigation and remediation for customer data quality. A bounded Investigator Agent gathers evidence with read-only tools, a deterministic policy layer validates every proposal, and Streamlit pauses for human approval before any golden-record write.

This repository implements the Saturday MVP described in `spec_draft.md`: address reconciliation plus schema-impact reporting. It does not auto-migrate schemas and it never writes without a persisted approval.

## What you can demo

1. **Happy path.** `INC-ADDR-012` has a stale golden address. Investigate, inspect CRM/Billing/Support evidence, approve, and verify the golden record plus audit trail.
2. **Ambiguous / reject path.** `INC-ADDR-018` has three different valid streets. The agent escalates. Rejecting leaves golden data unchanged.
3. **Schema impact path.** `INC-SCHEMA-TYPE` (and `INC-SCHEMA-RENAME`) show a breaking contract diff and downstream lineage. Acknowledge the report; nothing is migrated.

## Setup

Python 3.12 is required.

```bash
cd capstone_project
python3.12 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
cp .env.example .env
streamlit run app.py
```

Optional live services in `.env`:

```text
OPENAI_API_KEY=...
ARIZE_SPACE_ID=...
ARIZE_API_KEY=...
ARIZE_PROJECT_NAME=data-steward-ai
ARIZE_COLLECTOR_ENDPOINT=https://otlp.arize.com
```

If `OPENAI_API_KEY` is missing, the investigator uses a deterministic fallback. If Arize space/API key are missing, tracing is skipped and the UI still runs.

## Seed and reset

```bash
data-steward-seed
# or
python -m data_steward.seed
```

This recreates `data/data_steward.db` with 18 customers × 4 systems, 20 incidents, lineage, and one pre-approved fixture (`PROP-SEED-001`). The Streamlit sidebar **Reset / reseed demo data** button does the same and also drops LangGraph checkpoints.

## Run the UI

```bash
streamlit run app.py
```

Inbox is a sidebar **work queue**. Click a pinned demo incident → **Run investigation** → review the comparison or schema diff → Approve / Edit / Reject / Acknowledge. Open **Activity** in the top nav for counts and the audit trail. Each investigation stores a Phoenix trace id when tracing is enabled.

## Evaluations

```bash
data-steward-eval
# optional LLM grounding judge (skipped if OPENAI_API_KEY is unset):
data-steward-eval --judge
# or
python -m data_steward.evaluations
```

CI / pytest is the **code-based bar** (deterministic fallback, no LLM judge):

- classification accuracy
- tool-selection accuracy
- grounding pass rate (heuristic: candidate values appear in retrieved records)
- correct escalation rate
- unauthorized modification count (must stay `0`)

`data-steward-eval --judge` adds a **subjective** `gpt-4o-mini` PASS/FAIL on evidence grounding only. Heuristic grounding stays reported; `combined_grounding_pass_rate` requires both heuristic and judge PASS. The judge is never used for unauthorized writes. If the API key is missing, the judge is skipped and the code-based metrics still run.

When Arize is configured, each judged golden case is also traced to project `data-steward-ai` with `eval.llm_grounding` and `eval.heuristic_grounding` scores on the span. Optionally `pip install -e ".[evals]"` installs the Arize SDK so those scores also fill the Evaluations column (`arize_eval_status=published`). Arize does not replace the judge model; it stores and displays the scores.

Golden cases live in `evals/golden_cases.json`.

## Tests

```bash
pytest
```

Covered paths include contract validation, schema diffs, approval enforcement, idempotent remediation, the three demo workflows, and the evaluation bar.

## Offline fallback

The demo is designed to survive a missing or failing OpenAI key:

- The investigator selects the same read-only tools and emits a structured recommendation from contract authority + the mock address validator.
- Human approval, remediation, verification, and audit are fully deterministic.
- Evaluations use the fallback path in CI so scores are repeatable.

## Arize AX tracing

Traces are sent to the Arize AX space in `.env` (`ARIZE_SPACE_ID` + `ARIZE_API_KEY`) via `https://otlp.arize.com`.

1. Confirm Space ID and API key from Arize **Space Settings**.
2. Keep `ARIZE_PROJECT_NAME=data-steward-ai` (or change it to the project you want in that space).
3. Restart Streamlit so instrumentation loads before LangGraph.
4. Run an investigation, then open [app.arize.com](https://app.arize.com) and select project `data-steward-ai` in that same space.

The UI shows the trace id and project link when tracing is enabled. Phoenix Cloud env vars still work as a fallback if Arize AX vars are absent.

To attach LLM-judge scores to those traces:

```bash
data-steward-eval --judge
```

Open the same Arize project and look for `operation=golden_eval` spans. Each span has `eval.llm_grounding.label` (`PASS`/`FAIL`). Unauthorized-write checks stay code-based and are not sent to the judge.

## Five-minute rehearsal

1. Reset/seed data from the sidebar.
2. Click pinned `INC-ADDR-012` in the queue. Show the CRM/Billing/Support/golden comparison table.
3. Run investigation. Walk the proposed change, evidence citations, and policy line.
4. Approve. Show before/after on the same page.
5. Click `INC-ADDR-018`. Show the escalation and reject it. Golden stays unchanged.
6. Click `INC-SCHEMA-TYPE`. Show the type/rename diff, downstream chips, and acknowledge with no migration.
7. Open **Activity** in the top nav, then the Arize AX project `data-steward-ai` in the same space.

Saturday morning: reseed, confirm `.env` keys, run `pytest` plus one Streamlit smoke path, and keep this README plus eval output as fallback.

## Architecture notes

- Package: `src/data_steward`
- Only write path: `RemediationService.apply`, which requires a persisted `approved` decision
- Investigator tools are read-only; unauthorized tool names are blocked
- LangGraph checkpoints are stored separately from application tables (`data/checkpoints.db`)
- Resume uses a stable thread id equal to the incident id
