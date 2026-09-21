from __future__ import annotations

from pathlib import Path
from typing import Any

SKILL_BY_INCIDENT = {
    "address_discrepancy": "reconcile-address",
    "schema_drift": "schema-impact",
}


def skills_root(path: str | Path | None = None) -> Path:
    if path is not None:
        return Path(path)
    return Path(__file__).resolve().parents[2] / "skills"


def prompt_catalog(root: str | Path | None = None) -> list[dict[str, str]]:
    """Names and descriptions for the investigator prompt (Assignment 1 catalog)."""
    catalog: list[dict[str, str]] = []
    base = skills_root(root)
    if not base.is_dir():
        return catalog
    for skill_dir in sorted(base.iterdir()):
        skill_file = skill_dir / "SKILL.md"
        if not skill_file.is_file():
            continue
        parsed = _parse_skill(skill_file)
        catalog.append(
            {
                "name": parsed["name"] or skill_dir.name,
                "description": parsed["description"],
            }
        )
    return catalog


def skill_for_incident(incident_type: str) -> str | None:
    return SKILL_BY_INCIDENT.get(incident_type)


def load_skill(name: str, root: str | Path | None = None) -> dict[str, Any]:
    """Return one skill body. Unknown names error; never invent a procedure."""
    allowed = {item["name"] for item in prompt_catalog(root)}
    if name not in allowed:
        return {"error": f"unknown skill: {name}", "catalog": sorted(allowed)}
    skill_file = skills_root(root) / name / "SKILL.md"
    parsed = _parse_skill(skill_file)
    return {
        "name": parsed["name"] or name,
        "description": parsed["description"],
        "body": parsed["body"],
    }


def _parse_skill(path: Path) -> dict[str, str]:
    text = path.read_text(encoding="utf-8")
    name = path.parent.name
    description = ""
    body = text
    if text.startswith("---"):
        parts = text.split("---", 2)
        if len(parts) >= 3:
            frontmatter = parts[1]
            body = parts[2].strip()
            for line in frontmatter.splitlines():
                if line.startswith("name:"):
                    name = line.split(":", 1)[1].strip()
                elif line.startswith("description:"):
                    description = line.split(":", 1)[1].strip()
    return {"name": name, "description": description, "body": body}
