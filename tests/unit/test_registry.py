from pathlib import Path

from fleet.registry import AgentDef, namespace_id, scan_directory

FIXTURES = Path(__file__).parent.parent / "fixtures" / "registry_snapshots"


def test_namespace_id() -> None:
    assert namespace_id("ruflo", "coder") == "ruflo:coder"


def test_scan_ruflo_directory_finds_md_agents() -> None:
    defs = scan_directory(FIXTURES / "sample_ruflo", source="ruflo", pattern="*.md")
    assert len(defs) == 1
    d = defs[0]
    assert d.id == "ruflo:coder"
    assert d.name == "coder"
    assert d.source == "ruflo"
    assert "implementation" in d.description.lower()


def test_scan_superpowers_finds_skill_md_under_subdirs() -> None:
    defs = scan_directory(
        FIXTURES / "sample_superpowers", source="superpowers", pattern="*/SKILL.md"
    )
    assert len(defs) == 1
    assert defs[0].id == "superpowers:tdd-guide"


def test_agentdef_role_tags_extracted_from_description() -> None:
    d = AgentDef(
        id="x:y",
        name="y",
        source="x",
        description="Test-driven development specialist for Python",
        path="/tmp/y",
        model=None,
        tools=[],
    )
    tags = d.role_tags()
    assert "test" in tags
    assert "python" in tags
