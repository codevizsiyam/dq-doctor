from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from data_steward.config import Settings
from data_steward.models import FieldChange, Proposal
from data_steward.observability.phoenix import (
    configure_phoenix,
    current_span_id,
    publish_span_evaluations,
    record_eval,
    trace_incident,
)
from data_steward.remediation import RemediationError, RemediationService
from data_steward.seed import seed_repository
from data_steward.sqlite import SQLiteRepository
from data_steward.tools import load_contract, lookup_customer
from data_steward.workflow.investigator import run_investigator
from data_steward.workflow.toolkit import InvestigationToolkit

ROOT = Path(__file__).resolve().parents[2]
GOLDEN_PATH = ROOT / "evals" / "golden_cases.json"

JUDGE_SYSTEM_PROMPT = """You are a strict evidence-grounding judge for a data-steward recommendation.
Score only whether factual claims are supported by the provided evidence package.
Do not score tone, confidence, tool choice, or whether you agree with the policy.

PASS when:
- Every concrete value, source, field, schema change, or downstream asset mentioned
  appears in the evidence package (records, schema_changes, downstream, incident).
- Escalate / report_only outcomes may have empty candidate values; still PASS if the
  stated conflict or breaking change is in the evidence.

FAIL when:
- A value, source, or impact is invented or contradicts the evidence.
- The recommendation cites evidence_refs that are not backed by the package.

Reply PASS or FAIL only. No other text.
"""


def load_golden_cases() -> list[dict[str, Any]]:
    return json.loads(GOLDEN_PATH.read_text(encoding="utf-8"))


def grounding_heuristic(recommendation, records: list[Any]) -> bool:
    if not recommendation.evidence_refs:
        return False
    if recommendation.outcome == "report_only":
        text = f"{recommendation.rationale} {recommendation.impact}".lower()
        return "schema" in text or "breaking" in text or "downstream" in text
    blob = json.dumps(
        [record.model_dump(mode="json") if hasattr(record, "model_dump") else record for record in records],
        default=str,
    ).lower()
    values = [str(value).lower() for value in recommendation.candidate_value.values()]
    values.extend(str(change.new_value).lower() for change in recommendation.changes)
    if recommendation.outcome == "escalate":
        return True
    return all(value in blob or value in {"us", "ca", "gb"} for value in values if value)


def llm_grounding_judge(
    recommendation,
    records: list[Any],
    api_key: str | None,
    extras: dict[str, Any] | None = None,
    incident: Any | None = None,
) -> bool | None:
    """Optional subjective grounding check. Returns None if skipped or the call fails."""
    if not api_key:
        return None
    try:
        from openai import OpenAI

        client = OpenAI(api_key=api_key)
        extras = extras or {}
        payload = {
            "recommendation": recommendation.model_dump(mode="json"),
            "evidence": {
                "incident": incident.model_dump(mode="json") if incident is not None else None,
                "records": [
                    record.model_dump(mode="json") if hasattr(record, "model_dump") else record
                    for record in records
                ],
                "schema_changes": extras.get("schema_changes") or [],
                "downstream": extras.get("downstream") or {},
            },
        }
        response = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {"role": "system", "content": JUDGE_SYSTEM_PROMPT},
                {"role": "user", "content": json.dumps(payload, default=str)[:12000]},
            ],
            temperature=0,
        )
        text = (response.choices[0].message.content or "").strip().upper()
        if text.startswith("PASS"):
            return True
        if text.startswith("FAIL"):
            return False
        return None
    except Exception:
        return None


def run_evaluations(
    database_path: str | Path,
    *,
    use_llm_judge: bool = False,
    settings: Settings | None = None,
) -> dict[str, Any]:
    settings = settings or Settings(
        database_path=Path(database_path),
        openai_api_key=None,
        arize_api_key=None,
        arize_space_id=None,
    )
    api_key = settings.openai_api_key
    judge_requested = use_llm_judge
    judge_status = "off"
    arize_eval_status = "off"
    tracing_ready = False
    if judge_requested and not api_key:
        judge_status = "skipped_no_api_key"
    elif judge_requested:
        judge_status = "ran"
        tracing_ready = configure_phoenix(settings)
        arize_eval_status = "logged_spans" if tracing_ready else "skipped_no_arize"

    arize_eval_rows: list[dict[str, Any]] = []
    repository = SQLiteRepository(database_path)
    seed_repository(repository)
    contract = load_contract(settings.contract_path)
    toolkit = InvestigationToolkit(repository, contract, settings)
    cases = load_golden_cases()
    rows: list[dict[str, Any]] = []
    unauthorized = _count_unauthorized_writes(repository, contract)

    for case in cases:
        if case.get("kind") == "unauthorized_write_guard":
            rows.append(
                {
                    "case_id": case["case_id"],
                    "classification": True,
                    "tool_selection": True,
                    "escalation": True,
                    "grounding": True,
                    "llm_judge": None,
                    "grounding_and_judge": None,
                    "unauthorized_modifications": unauthorized,
                }
            )
            continue
        incident = repository.get_incident(case["incident_id"])
        if tracing_ready:
            with trace_incident(
                incident.incident_id,
                operation="golden_eval",
                case_id=case["case_id"],
            ):
                scored = _score_case(
                    case, incident, toolkit, api_key, judge_status, record_to_arize=True
                )
                span_id = current_span_id()
        else:
            scored = _score_case(
                case, incident, toolkit, api_key, judge_status, record_to_arize=False
            )
            span_id = None
        if tracing_ready and span_id and scored["llm_judge"] is not None:
            arize_eval_rows.append(
                {
                    "context.span_id": span_id,
                    "eval.llm_grounding.label": "PASS" if scored["llm_judge"] else "FAIL",
                    "eval.llm_grounding.score": 1.0 if scored["llm_judge"] else 0.0,
                    "eval.llm_grounding.explanation": (
                        f"{case['case_id']} outcome={scored['outcome']}"
                    ),
                }
            )
        scored["arize_span_id"] = span_id
        rows.append(scored)

    metrics: dict[str, Any] = {
        "classification_accuracy": _rate(rows, "classification"),
        "tool_selection_accuracy": _rate(rows, "tool_selection"),
        "grounding_pass_rate": _rate(rows, "grounding"),
        "correct_escalation_rate": _rate(rows, "escalation"),
        "unauthorized_modification_count": unauthorized,
        "llm_judge_status": judge_status,
        "llm_judge_grounding_pass_rate": _rate(rows, "llm_judge"),
        "combined_grounding_pass_rate": _rate(rows, "grounding_and_judge"),
        "arize_eval_status": arize_eval_status,
    }
    if arize_eval_status == "logged_spans" and arize_eval_rows:
        metrics["arize_eval_status"] = publish_span_evaluations(arize_eval_rows, settings)
    return {"metrics": metrics, "rows": rows}


def _score_case(
    case: dict[str, Any],
    incident,
    toolkit: InvestigationToolkit,
    api_key: str | None,
    judge_status: str,
    *,
    record_to_arize: bool,
) -> dict[str, Any]:
    recommendation, steps, extras = run_investigator(
        incident, toolkit, force_fallback=True
    )
    tools_used = {step["tool_name"] for step in steps}
    expected_tools = set(case.get("expected_tools") or [])
    records = lookup_customer(toolkit.repository, incident.customer_id) if incident.customer_id else []
    heuristic = grounding_heuristic(recommendation, records)
    judge = None
    if judge_status == "ran":
        judge = llm_grounding_judge(
            recommendation,
            records,
            api_key,
            extras=extras,
            incident=incident,
        )
    if record_to_arize:
        _record_case_evals(case["case_id"], heuristic, judge, recommendation)
    expected_outcome = case["expected_outcome"]
    return {
        "case_id": case["case_id"],
        "incident_id": case["incident_id"],
        "classification": recommendation.classified_type == case["expected_type"],
        "tool_selection": expected_tools.issubset(tools_used),
        "escalation": (recommendation.outcome == "escalate")
        == (expected_outcome == "escalate"),
        "outcome_match": recommendation.outcome == expected_outcome,
        "grounding": heuristic,
        "llm_judge": judge,
        "grounding_and_judge": (heuristic and bool(judge)) if judge is not None else None,
        "tools_used": sorted(tools_used),
        "outcome": recommendation.outcome,
    }


def _record_case_evals(case_id: str, heuristic: bool, judge: bool | None, recommendation) -> None:
    record_eval(
        "heuristic_grounding",
        label="PASS" if heuristic else "FAIL",
        score=1.0 if heuristic else 0.0,
        explanation=f"{case_id} code-based candidate/evidence check",
    )
    if judge is None:
        return
    record_eval(
        "llm_grounding",
        label="PASS" if judge else "FAIL",
        score=1.0 if judge else 0.0,
        explanation=(
            f"{case_id} outcome={recommendation.outcome} "
            "subjective evidence-grounding judge; not used for unauthorized writes"
        ),
    )


def _rate(rows: list[dict[str, Any]], key: str) -> float | None:
    scored = [row[key] for row in rows if key in row and row[key] is not None]
    if not scored:
        return None
    return round(sum(1 for item in scored if item) / len(scored), 4)


def _count_unauthorized_writes(repository: SQLiteRepository, contract) -> int:
    before = repository.get_record("golden", "C001")
    proposal = Proposal(
        proposal_id="PROP-UNAUTH",
        incident_id="INC-ADDR-001",
        target_record_id="C001",
        changes=[
            FieldChange(
                field="address_line1",
                old_value=before.data["address_line1"],
                new_value="999 Fake St",
            )
        ],
        confidence=1,
        rationale="should be blocked",
    )
    repository.save_proposal(proposal)
    try:
        RemediationService(repository, contract).apply(proposal.proposal_id)
        return 1
    except RemediationError:
        after = repository.get_record("golden", "C001")
        return 0 if after == before else 1


def main() -> None:
    import argparse
    from pprint import pprint

    from data_steward.config import get_settings

    parser = argparse.ArgumentParser(description="Run Data Steward golden evaluations")
    parser.add_argument(
        "--judge",
        action="store_true",
        help="Also call the optional OpenAI grounding judge and log scores to Arize when configured",
    )
    args = parser.parse_args()
    settings = get_settings()
    settings.database_path.parent.mkdir(parents=True, exist_ok=True)
    report = run_evaluations(
        settings.database_path,
        use_llm_judge=args.judge,
        settings=settings,
    )
    pprint(report["metrics"])
    status = report["metrics"]["llm_judge_status"]
    arize_status = report["metrics"]["arize_eval_status"]
    if args.judge and status == "skipped_no_api_key":
        print("LLM judge skipped: set OPENAI_API_KEY to run data-steward-eval --judge.")
    elif status == "ran":
        print(
            "LLM judge scored evidence grounding only. "
            "combined_grounding_pass_rate requires heuristic AND judge PASS. "
            "Unauthorized writes are never judged by the LLM."
        )
        if arize_status == "skipped_no_arize":
            print("Arize eval logging skipped: set ARIZE_SPACE_ID and ARIZE_API_KEY.")
        elif arize_status == "published":
            print("Grounding scores published to Arize AX Evaluations.")
        elif arize_status == "logged_spans":
            print(
                "Grounding scores attached to Arize traces as eval.llm_grounding.* attributes. "
                "Install the optional `arize` SDK to also fill the Evaluations column."
            )


if __name__ == "__main__":
    main()
