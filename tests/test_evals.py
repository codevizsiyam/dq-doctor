from contextlib import contextmanager
from unittest.mock import MagicMock, patch

from data_steward.config import Settings
from data_steward.evaluations import llm_grounding_judge, run_evaluations
from data_steward.models import Recommendation
from data_steward.observability.phoenix import publish_span_evaluations


def test_golden_evaluations_meet_demo_bar(tmp_path):
    report = run_evaluations(tmp_path / "eval.db")
    metrics = report["metrics"]
    assert metrics["classification_accuracy"] == 1
    assert metrics["tool_selection_accuracy"] == 1
    assert metrics["correct_escalation_rate"] == 1
    assert metrics["grounding_pass_rate"] == 1
    assert metrics["unauthorized_modification_count"] == 0
    assert metrics["llm_judge_status"] == "off"
    assert metrics["llm_judge_grounding_pass_rate"] is None
    assert metrics["combined_grounding_pass_rate"] is None
    assert metrics["arize_eval_status"] == "off"
    assert len(report["rows"]) >= 20
    assert all(row.get("llm_judge") is None for row in report["rows"])


def test_judge_skipped_without_api_key(tmp_path):
    report = run_evaluations(
        tmp_path / "eval.db",
        use_llm_judge=True,
        settings=Settings(
            database_path=tmp_path / "eval.db",
            openai_api_key=None,
            arize_api_key=None,
            arize_space_id=None,
        ),
    )
    assert report["metrics"]["llm_judge_status"] == "skipped_no_api_key"
    assert report["metrics"]["grounding_pass_rate"] == 1
    assert report["metrics"]["llm_judge_grounding_pass_rate"] is None
    assert report["metrics"]["combined_grounding_pass_rate"] is None
    assert report["metrics"]["unauthorized_modification_count"] == 0
    assert report["metrics"]["arize_eval_status"] == "off"


def _judge_settings(tmp_path) -> Settings:
    return Settings(
        database_path=tmp_path / "eval.db",
        openai_api_key="sk-test",
        arize_api_key=None,
        arize_space_id=None,
    )


def test_judge_pass_is_additional_metric(tmp_path):
    with patch("openai.OpenAI") as openai_cls:
        openai_cls.return_value.chat.completions.create.return_value = _completion("PASS")
        report = run_evaluations(
            tmp_path / "eval.db",
            use_llm_judge=True,
            settings=_judge_settings(tmp_path),
        )
    metrics = report["metrics"]
    assert metrics["grounding_pass_rate"] == 1
    assert metrics["llm_judge_status"] == "ran"
    assert metrics["llm_judge_grounding_pass_rate"] == 1
    assert metrics["combined_grounding_pass_rate"] == 1
    assert metrics["unauthorized_modification_count"] == 0
    assert metrics["arize_eval_status"] == "skipped_no_arize"
    judged = [row for row in report["rows"] if row.get("llm_judge") is not None]
    assert judged
    assert all(row["grounding"] and row["llm_judge"] and row["grounding_and_judge"] for row in judged)


def test_judge_fail_does_not_change_heuristic_bar(tmp_path):
    with patch("openai.OpenAI") as openai_cls:
        openai_cls.return_value.chat.completions.create.return_value = _completion("FAIL")
        report = run_evaluations(
            tmp_path / "eval.db",
            use_llm_judge=True,
            settings=_judge_settings(tmp_path),
        )
    metrics = report["metrics"]
    assert metrics["grounding_pass_rate"] == 1
    assert metrics["llm_judge_grounding_pass_rate"] == 0
    assert metrics["combined_grounding_pass_rate"] == 0
    judged = [row for row in report["rows"] if row.get("llm_judge") is not None]
    assert judged
    assert all(row["grounding"] and row["llm_judge"] is False for row in judged)


def test_llm_grounding_judge_parses_pass_and_skips_without_key():
    recommendation = Recommendation(
        outcome="recommend",
        incident_id="INC-ADDR-001",
        classified_type="address_discrepancy",
        confidence=0.9,
        uncertainty="",
        impact="test",
        rationale="Billing has 10 Main St",
        evidence_refs=["billing:billing-C001:address_line1"],
        candidate_value={"address_line1": "10 Main St"},
    )
    assert llm_grounding_judge(recommendation, [], None) is None
    with patch("openai.OpenAI") as openai_cls:
        openai_cls.return_value.chat.completions.create.return_value = _completion("PASS grounded")
        assert llm_grounding_judge(recommendation, [], "sk-test") is True


@contextmanager
def _fake_trace(*_args, **_kwargs):
    yield MagicMock()


def test_judge_logs_scores_to_arize(tmp_path):
    settings = Settings(
        database_path=tmp_path / "eval.db",
        openai_api_key="sk-test",
        arize_api_key="ak-test",
        arize_space_id="space-test",
    )
    with (
        patch("data_steward.evaluations.configure_phoenix", return_value=True),
        patch("data_steward.evaluations.trace_incident", _fake_trace),
        patch("data_steward.evaluations.current_span_id", return_value="span-1"),
        patch("data_steward.evaluations.record_eval") as recorded,
        patch("data_steward.evaluations.publish_span_evaluations", return_value="published") as publish,
        patch("openai.OpenAI") as openai_cls,
    ):
        openai_cls.return_value.chat.completions.create.return_value = _completion("PASS")
        report = run_evaluations(
            tmp_path / "eval.db",
            use_llm_judge=True,
            settings=settings,
        )
    assert report["metrics"]["arize_eval_status"] == "published"
    assert recorded.called
    uploaded = publish.call_args[0][0]
    assert uploaded
    assert uploaded[0]["eval.llm_grounding.label"] == "PASS"
    assert uploaded[0]["context.span_id"] == "span-1"


def test_publish_span_evaluations_skips_without_arize_keys():
    settings = Settings(arize_api_key=None, arize_space_id=None)
    assert publish_span_evaluations([{"context.span_id": "x"}], settings) == "skipped_no_arize"


def _completion(text: str) -> MagicMock:
    response = MagicMock()
    response.choices = [MagicMock()]
    response.choices[0].message.content = text
    return response
