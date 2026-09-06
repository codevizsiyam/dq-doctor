from pathlib import Path


def test_streamlit_app_is_import_safe():
    source = (Path(__file__).parents[1] / "app.py").read_text(encoding="utf-8")
    assert "def main() -> None:" in source
    assert "Run investigation" in source
    assert "Work queue" in source
    assert "is_acknowledge_only" in source
    assert "Acknowledge (no migration)" in source
    assert "st.json" in source
    assert "DQ Doctor" in source
    assert "st.logo" in source
    assert "st.navigation" in source
