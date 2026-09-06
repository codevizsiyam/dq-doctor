from unittest.mock import patch

from langgraph.checkpoint.memory import InMemorySaver

from data_steward.config import Settings
from data_steward.models import FieldChange, HumanDecision, Recommendation
from data_steward.seed import reset_database
from data_steward.workflow.service import StewardRuntime


def _runtime(tmp_path) -> StewardRuntime:
    settings = Settings(
        database_path=tmp_path / "steward.db",
        checkpoint_path=tmp_path / "checkpoints.db",
        openai_api_key=None,
        arize_api_key=None,
        arize_space_id=None,
    )
    repository = reset_database(settings.database_path)
    return StewardRuntime(
        repository,
        settings,
        force_fallback=True,
        checkpointer=InMemorySaver(),
    )


def test_happy_path_approve_remediates_and_is_idempotent(tmp_path):
    runtime = _runtime(tmp_path)
    view = runtime.investigate("INC-ADDR-012")
    assert view.interrupted
    assert view.recommendation.outcome == "recommend"
    assert view.recommendation.changes
    assert view.recommendation.evidence_refs

    approved = runtime.decide(
        "INC-ADDR-012",
        HumanDecision(action="approve", reviewer="pytest", comment="looks correct"),
    )
    assert approved.remediation["applied"]
    golden = runtime.repository.get_record("golden", "C012")
    assert golden.data["address_line1"] == "63 Hill Street"
    assert runtime.repository.get_incident("INC-ADDR-012").status == "resolved"

    replay = runtime.remediator.apply(approved.proposal_id)
    assert replay.idempotent_replay


def test_ambiguous_case_escalates_and_reject_does_not_write(tmp_path):
    runtime = _runtime(tmp_path)
    before = runtime.repository.get_record("golden", "C018")
    view = runtime.investigate("INC-ADDR-018")
    assert view.recommendation.outcome == "escalate"
    rejected = runtime.decide(
        "INC-ADDR-018",
        HumanDecision(action="reject", reviewer="pytest", comment="needs a human match"),
    )
    assert rejected.status == "rejected"
    assert runtime.repository.get_record("golden", "C018") == before
    assert rejected.remediation is None


def test_schema_path_reports_impact_and_does_not_migrate(tmp_path):
    runtime = _runtime(tmp_path)
    before = [
        runtime.repository.get_record("golden", f"C{index:03d}").data
        for index in range(1, 19)
    ]
    view = runtime.investigate("INC-SCHEMA-TYPE")
    assert view.recommendation.outcome == "report_only"
    assert view.schema_changes
    assert view.downstream
    done = runtime.decide(
        "INC-SCHEMA-TYPE",
        HumanDecision(action="acknowledge", reviewer="pytest", comment="do not migrate"),
    )
    assert done.status == "acknowledged"
    after = [
        runtime.repository.get_record("golden", f"C{index:03d}").data
        for index in range(1, 19)
    ]
    assert after == before


def test_schema_live_recommend_is_forced_report_only(tmp_path):
    runtime = _runtime(tmp_path)
    fake = Recommendation(
        outcome="recommend",
        incident_id="INC-SCHEMA-TYPE",
        classified_type="address_discrepancy",
        target_record_id="C001",
        changes=[FieldChange(field="address_line1", old_value="a", new_value="b")],
        confidence=0.9,
        uncertainty="",
        impact="would write",
        rationale="model tried to approve a golden write",
        candidate_value={"address_line1": "b"},
        tools_used=["diff_schemas"],
        fallback_used=False,
    )
    with patch(
        "data_steward.workflow.graph.run_investigator",
        return_value=(fake, [], {"schema_changes": [{"field": "customer_id"}], "downstream": {}}),
    ):
        view = runtime.investigate("INC-SCHEMA-TYPE")
    assert view.recommendation.outcome == "report_only"
    assert view.recommendation.changes == []
    assert view.proposal_id is None
    done = runtime.decide(
        "INC-SCHEMA-TYPE",
        HumanDecision(action="approve", reviewer="pytest", comment="should still acknowledge"),
    )
    assert done.status == "acknowledged"
    assert done.remediation is None
