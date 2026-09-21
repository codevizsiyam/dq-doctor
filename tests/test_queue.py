from pathlib import Path

from data_steward.config import Settings
from data_steward.queue import run_queue_lookups
from data_steward.seed import seed_repository
from data_steward.sqlite import SQLiteRepository
from data_steward.tools import load_contract
from data_steward.workflow.toolkit import InvestigationToolkit

ROOT = Path(__file__).parents[1]


def test_queue_lookups_are_read_only_and_authorized(tmp_path):
    settings = Settings(
        database_path=tmp_path / "steward.db",
        checkpoint_path=tmp_path / "checkpoints.db",
        openai_api_key=None,
        arize_api_key=None,
        arize_space_id=None,
    )
    repository = SQLiteRepository(settings.database_path)
    seed_repository(repository)
    before = [repository.get_record("golden", f"C{index:03d}").data for index in range(1, 19)]
    toolkit = InvestigationToolkit(repository, load_contract(ROOT / "contracts/customer.yaml"), settings)

    report = run_queue_lookups(toolkit, limit=20)

    assert report["incidents"] == 20
    assert report["errors"] == 0
    assert report["unauthorized_tools"] == 0
    assert report["writes"] == 0
    assert all(row["authorized"] for row in report["rows"])
    assert {row["incident_id"] for row in report["rows"]} >= {"INC-ADDR-012", "INC-ADDR-018", "INC-SCHEMA-TYPE"}
    blocked, step, ok = toolkit.execute("apply_write", {})
    assert not ok
    assert blocked["error"].startswith("unauthorized tool")
    after = [repository.get_record("golden", f"C{index:03d}").data for index in range(1, 19)]
    assert after == before
