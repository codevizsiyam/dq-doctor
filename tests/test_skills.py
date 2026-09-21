from pathlib import Path

from data_steward.config import Settings
from data_steward.seed import reset_database
from data_steward.skills import load_skill, prompt_catalog, skill_for_incident
from data_steward.tools import load_contract
from data_steward.workflow.fallback import fallback_recommendation
from data_steward.workflow.investigator import reload_skill_message
from data_steward.workflow.toolkit import OPENAI_TOOL_SPECS, READ_ONLY_TOOLS, InvestigationToolkit

ROOT = Path(__file__).parents[1]
SKILLS = ROOT / "skills"


def test_catalog_lists_both_skills():
    names = {item["name"] for item in prompt_catalog(SKILLS)}
    assert names == {"reconcile-address", "schema-impact"}
    assert skill_for_incident("address_discrepancy") == "reconcile-address"
    assert skill_for_incident("schema_drift") == "schema-impact"


def test_load_skill_returns_procedure_and_rejects_unknown():
    address = load_skill("reconcile-address", SKILLS)
    assert address["name"] == "reconcile-address"
    assert "lookup_customer" in address["body"]
    assert "escalate" in address["body"]

    schema = load_skill("schema-impact", SKILLS)
    assert "report_only" in schema["body"]
    assert "diff_schemas" in schema["body"]

    unknown = load_skill("not-a-skill", SKILLS)
    assert unknown["error"].startswith("unknown skill")
    assert "reconcile-address" in unknown["catalog"]


def test_toolkit_load_skill_is_read_only_and_blocks_writes(tmp_path):
    toolkit = _toolkit(tmp_path)
    payload, step, authorized = toolkit.execute("load_skill", {"name": "schema-impact"})
    assert authorized
    assert payload["name"] == "schema-impact"
    assert "report_only" in payload["body"]
    assert "loaded skill schema-impact" in step.result_summary
    assert "load_skill" in READ_ONLY_TOOLS

    blocked, blocked_step, ok = toolkit.execute("apply_write", {})
    assert not ok
    assert blocked["error"].startswith("unauthorized tool")
    assert not blocked_step.authorized
    spec_names = [item["function"]["name"] for item in OPENAI_TOOL_SPECS]
    assert spec_names == list(READ_ONLY_TOOLS)


def test_fallback_records_load_skill_first(tmp_path):
    toolkit = _toolkit(tmp_path)
    address = toolkit.repository.get_incident("INC-ADDR-012")
    _, address_steps, _ = fallback_recommendation(address, toolkit)
    assert address_steps[0]["tool_name"] == "load_skill"
    assert address_steps[0]["arguments"]["name"] == "reconcile-address"

    schema = toolkit.repository.get_incident("INC-SCHEMA-TYPE")
    _, schema_steps, _ = fallback_recommendation(schema, toolkit)
    assert schema_steps[0]["tool_name"] == "load_skill"
    assert schema_steps[0]["arguments"]["name"] == "schema-impact"

    message = reload_skill_message(address, toolkit)
    assert message["role"] == "user"
    assert "reconcile-address" in message["content"]


def _toolkit(tmp_path) -> InvestigationToolkit:
    settings = Settings(
        database_path=tmp_path / "steward.db",
        checkpoint_path=tmp_path / "checkpoints.db",
        skills_path=SKILLS,
        openai_api_key=None,
        arize_api_key=None,
        arize_space_id=None,
    )
    repository = reset_database(settings.database_path)
    contract = load_contract(ROOT / "contracts/customer.yaml")
    return InvestigationToolkit(repository, contract, settings)
