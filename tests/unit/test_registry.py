from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from fleet.registry import AgentDef, Registry, RegistryConfig, namespace_id, scan_directory

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


def make_cfg(tmp_path: Path) -> RegistryConfig:
    (tmp_path / "ruflo").mkdir()
    (tmp_path / "ruflo" / "x.md").write_text("---\nname: x\ndescription: y\n---\n")
    return RegistryConfig(
        sources=[
            {"name": "ruflo", "root": str(tmp_path / "ruflo"), "pattern": "*.md"},
        ],
    )


@pytest.mark.asyncio
async def test_load_populates_index(tmp_path: Path) -> None:
    cfg = make_cfg(tmp_path)
    g = AsyncMock()
    g.add_episode = AsyncMock(return_value="ep")
    g.search_facts = AsyncMock(return_value=[])
    r = Registry(cfg, graphiti=g)
    await r.load()
    assert r.size() == 1
    assert r.get("ruflo:x") is not None


@pytest.mark.asyncio
async def test_lookup_unknown_returns_none(tmp_path: Path) -> None:
    cfg = make_cfg(tmp_path)
    g = AsyncMock()
    g.add_episode = AsyncMock()
    g.search_facts = AsyncMock(return_value=[])
    r = Registry(cfg, graphiti=g)
    await r.load()
    assert r.get("ruflo:nope") is None


@pytest.mark.asyncio
async def test_score_prefers_role_tag_match(tmp_path: Path) -> None:
    (tmp_path / "ruflo").mkdir()
    (tmp_path / "ruflo" / "rust.md").write_text("---\nname: rust\ndescription: rust expert\n---")
    (tmp_path / "ruflo" / "py.md").write_text("---\nname: py\ndescription: python expert\n---")
    cfg = RegistryConfig(
        sources=[{"name": "ruflo", "root": str(tmp_path / "ruflo"), "pattern": "*.md"}]
    )
    g = AsyncMock()
    g.add_episode = AsyncMock()
    g.search_facts = AsyncMock(return_value=[])
    r = Registry(cfg, graphiti=g)
    await r.load()
    ranked = r.score_for(task="fix rust borrow checker", limit=2)
    assert ranked[0].id == "ruflo:rust"


@pytest.mark.asyncio
async def test_score_tie_breaker_prefers_cheaper_model(tmp_path: Path) -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "a.md").write_text(
        "---\nname: a\ndescription: rust expert\nmodel: opus\n---"
    )
    (tmp_path / "src" / "b.md").write_text(
        "---\nname: b\ndescription: rust expert\nmodel: haiku\n---"
    )
    cfg = RegistryConfig(
        sources=[{"name": "src", "root": str(tmp_path / "src"), "pattern": "*.md"}]
    )
    g = AsyncMock()
    g.add_episode = AsyncMock()
    g.search_facts = AsyncMock(return_value=[])
    r = Registry(cfg, graphiti=g)
    await r.load()
    ranked = r.score_for(task="rust", limit=2)
    assert ranked[0].name == "b"  # haiku cheaper than opus


@pytest.mark.asyncio
async def test_load_falls_back_to_snapshot_on_missing_root(tmp_path: Path) -> None:
    cfg = RegistryConfig(
        sources=[{"name": "ruflo", "root": str(tmp_path / "missing"), "pattern": "*.md"}]
    )
    g = AsyncMock()
    g.add_episode = AsyncMock(return_value="ep")
    g.search_facts = AsyncMock(
        return_value=[
            {
                "body": {
                    "kind": "fleet_registry_snapshot",
                    "agents": [
                        {
                            "id": "ruflo:x",
                            "name": "x",
                            "source": "ruflo",
                            "description": "y",
                            "path": "/old/x.md",
                            "model": None,
                            "tools": [],
                        },
                    ],
                },
            }
        ]
    )
    r = Registry(cfg, graphiti=g)
    await r.load()
    assert r.size() == 1
    assert r.is_stale() is True
