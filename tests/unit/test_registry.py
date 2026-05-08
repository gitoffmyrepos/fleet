from pathlib import Path
from unittest.mock import patch

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


def test_skill_md_falls_back_to_parent_dir_name() -> None:
    defs = scan_directory(FIXTURES / "sample_anonymous", source="anon", pattern="*/SKILL.md")
    assert len(defs) == 1
    assert defs[0].name == "anonymous-skill"
    assert defs[0].id == "anon:anonymous-skill"


def test_scan_directory_skips_unreadable_files(tmp_path: Path) -> None:
    p = tmp_path / "broken.md"
    p.write_text("---\nname: x\n---\n")
    with patch.object(Path, "read_text", side_effect=OSError("permission denied")):
        defs = scan_directory(tmp_path, source="x", pattern="*.md")
    assert defs == []


def test_scan_directory_missing_root_returns_empty(tmp_path: Path) -> None:
    defs = scan_directory(tmp_path / "does-not-exist", source="x", pattern="*.md")
    assert defs == []


def test_file_without_frontmatter_uses_path_stem(tmp_path: Path) -> None:
    (tmp_path / "no-frontmatter.md").write_text("just body content, no frontmatter\n")
    defs = scan_directory(tmp_path, source="src", pattern="*.md")
    assert len(defs) == 1
    assert defs[0].name == "no-frontmatter"
    assert defs[0].description == ""
