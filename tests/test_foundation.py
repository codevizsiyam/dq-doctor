from pathlib import Path

from data_steward.models import SourceSystem
from data_steward.seed import seed_repository
from data_steward.sqlite import SQLiteRepository
from data_steward.tools import compare_records, load_contract, lookup_lineage, validate_address

ROOT = Path(__file__).parents[1]


def test_seed_is_deterministic_and_complete(tmp_path):
    repository = SQLiteRepository(tmp_path / "steward.db")
    seed_repository(repository)

    all_records = [
        record
        for number in range(1, 19)
        for record in repository.find_records(f"C{number:03d}")
    ]
    assert len(all_records) == 72
    assert {record.source for record in all_records} == {
        source.value for source in SourceSystem
    }
    assert len(lookup_lineage(repository, "C001")) == 3
    assert repository.get_incident("INC-SCHEMA-TYPE") is not None
    assert repository.get_incident("INC-SCHEMA-RENAME") is not None

    first_snapshot = [record.model_dump_json() for record in all_records]
    seed_repository(repository)
    second_snapshot = [
        record.model_dump_json()
        for number in range(1, 19)
        for record in repository.find_records(f"C{number:03d}")
    ]
    assert first_snapshot == second_snapshot


def test_read_only_comparison_and_address_validator(tmp_path):
    repository = SQLiteRepository(tmp_path / "steward.db")
    seed_repository(repository)
    comparison = compare_records(repository.find_records("C003"))

    assert comparison.conflicting_fields == ["zip_code"]
    result = validate_address(
        {
            "address_line1": "10 Main Street",
            "city": "Austin",
            "state": "TX",
            "zip_code": "73301",
            "country": "US",
        }
    )
    assert result.valid
    assert result.normalized["address_line1"] == "10 MAIN ST"
    assert not validate_address({}).valid
    assert load_contract(ROOT / "contracts/customer.yaml").entity == "customer"
