"""Tests for fleet/skills.py — unified skill catalog discovery."""

from __future__ import annotations

from pathlib import Path

import pytest

from fleet import skills as fleet_skills


def _write_skill(
    root: Path,
    category: str,
    name: str,
    *,
    description: str = "",
    tags: list[str] | None = None,
    mcp_servers: list[str] | None = None,
) -> Path:
    """Materialize a SKILL.md under <root>/<category>/<name>/."""
    p = root / category / name
    p.mkdir(parents=True, exist_ok=True)
    fm_lines = [f"name: {name}", f"description: {description}"]
    if tags:
        fm_lines.append("tags:")
        for t in tags:
            fm_lines.append(f"  - {t}")
    if mcp_servers:
        fm_lines.append("mcp_servers:")
        for m in mcp_servers:
            fm_lines.append(f"  - {m}")
    md = "---\n" + "\n".join(fm_lines) + "\n---\n# " + name + "\n\nBody.\n"
    (p / "SKILL.md").write_text(md, encoding="utf-8")
    return p / "SKILL.md"


@pytest.fixture(autouse=True)
def _reset_cache() -> None:
    fleet_skills.invalidate_cache()


@pytest.fixture
def fake_roots(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Path]:
    """Stand up fake hermes + claude skill roots under tmp_path."""
    hermes = tmp_path / "hermes-skills"
    claude = tmp_path / "claude-skills"
    marketplaces = tmp_path / "claude-plugins-marketplaces"
    hermes.mkdir()
    claude.mkdir()
    marketplaces.mkdir()
    monkeypatch.setattr(fleet_skills, "ROOTS", [hermes, claude])
    monkeypatch.setattr(fleet_skills, "MARKETPLACE_GLOB", marketplaces)
    monkeypatch.setattr(fleet_skills, "INDEX_HINT", tmp_path / "missing-index.json")
    return {"hermes": hermes, "claude": claude, "marketplaces": marketplaces}


@pytest.mark.asyncio
async def test_walk_discovers_skill_md(fake_roots: dict[str, Path]) -> None:
    _write_skill(fake_roots["hermes"], "research", "deep-think", description="Thinks deeply.")
    _write_skill(fake_roots["claude"], "swe", "tdd-guide", description="TDD enforcer.")
    catalog = await fleet_skills.load_catalog()
    names = sorted(s["name"] for s in catalog["skills"])
    assert names == ["deep-think", "tdd-guide"]
    assert len(catalog["roots"]) == 2


@pytest.mark.asyncio
async def test_filter_by_kind_matches_tag_or_description(
    fake_roots: dict[str, Path],
) -> None:
    _write_skill(
        fake_roots["hermes"],
        "qa",
        "regression-runner",
        description="Runs regression tests.",
        tags=["test", "verify"],
    )
    _write_skill(
        fake_roots["hermes"],
        "ops",
        "deploy-promoter",
        description="Promotes builds to staging.",
        tags=["deploy", "ship"],
    )
    _write_skill(
        fake_roots["hermes"],
        "writing",
        "doc-writer",
        description="Writes docs.",
        tags=["docs"],
    )
    catalog = await fleet_skills.load_catalog()
    verify_skills = fleet_skills.filter_skills(catalog, kind="verify")
    names = {s["name"] for s in verify_skills}
    assert "regression-runner" in names
    assert "deploy-promoter" not in names
    ship_skills = fleet_skills.filter_skills(catalog, kind="ship")
    assert {s["name"] for s in ship_skills} == {"deploy-promoter"}


@pytest.mark.asyncio
async def test_filter_by_mcp_server(fake_roots: dict[str, Path]) -> None:
    _write_skill(
        fake_roots["hermes"],
        "infra",
        "fleet-driver",
        description="Drives Fleet MCP.",
        mcp_servers=["fleet"],
    )
    _write_skill(
        fake_roots["hermes"],
        "infra",
        "k8s-driver",
        description="kubectl wrapper.",
        mcp_servers=["kubernetes"],
    )
    catalog = await fleet_skills.load_catalog()
    fleet_only = fleet_skills.filter_skills(catalog, mcp="fleet")
    assert [s["name"] for s in fleet_only] == ["fleet-driver"]


@pytest.mark.asyncio
async def test_dedup_by_name_lowercase(fake_roots: dict[str, Path]) -> None:
    """Same skill name in two roots only surfaces once."""
    _write_skill(fake_roots["hermes"], "swe", "tdd-guide", description="hermes copy")
    _write_skill(fake_roots["claude"], "swe", "TDD-Guide", description="claude copy")
    catalog = await fleet_skills.load_catalog()
    matches = [s for s in catalog["skills"] if s["name"].lower() == "tdd-guide"]
    assert len(matches) == 1


@pytest.mark.asyncio
async def test_hub_dir_excluded_from_walk(fake_roots: dict[str, Path]) -> None:
    _write_skill(fake_roots["hermes"], ".hub/quarantine", "shady", description="shady")
    _write_skill(fake_roots["hermes"], "real", "good", description="good")
    catalog = await fleet_skills.load_catalog()
    names = [s["name"] for s in catalog["skills"]]
    assert names == ["good"]


@pytest.mark.asyncio
async def test_marketplace_skills_discovered(fake_roots: dict[str, Path]) -> None:
    mp = fake_roots["marketplaces"] / "official"
    mp_skills = mp / "skills"
    mp_skills.mkdir(parents=True)
    _write_skill(mp_skills, "design", "color-picker", description="Picks colors.")
    catalog = await fleet_skills.load_catalog()
    assert any(s["name"] == "color-picker" for s in catalog["skills"])
    # Marketplace root is in the --add-dir-eligible roots list
    assert str(mp_skills) in catalog["roots"]


@pytest.mark.asyncio
async def test_render_prompt_header_compact(fake_roots: dict[str, Path]) -> None:
    _write_skill(
        fake_roots["hermes"],
        "x",
        "alpha",
        description="A" * 200,  # over the 120-char clamp
    )
    catalog = await fleet_skills.load_catalog()
    out = fleet_skills.render_prompt_header(catalog["skills"])
    assert "alpha:" in out
    # Description is clamped at 120 chars in the header.
    assert "A" * 121 not in out
    assert "Skills available" in out
    assert "--add-dir" in out


@pytest.mark.asyncio
async def test_empty_render_returns_empty(fake_roots: dict[str, Path]) -> None:
    assert fleet_skills.render_prompt_header([]) == ""


@pytest.mark.asyncio
async def test_limit_caps_results(fake_roots: dict[str, Path]) -> None:
    for i in range(20):
        _write_skill(fake_roots["hermes"], "cat", f"skill-{i:02d}", description="x")
    catalog = await fleet_skills.load_catalog()
    out = fleet_skills.filter_skills(catalog, limit=7)
    assert len(out) == 7


@pytest.mark.asyncio
async def test_index_hint_preferred_when_present(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When ~/.hermes/skills/.hub/index.json exists, it's loaded FIRST."""
    hub_dir = tmp_path / "hub"
    hub_dir.mkdir()
    index = hub_dir / "index.json"
    import json

    index.write_text(
        json.dumps(
            {
                "skills": [
                    {
                        "name": "from-hub",
                        "description": "Index-only entry",
                        "tags": ["hub"],
                        "mcp_servers": ["hub-server"],
                        "category": "test",
                        "path": "/imaginary/path",
                    }
                ]
            }
        )
    )
    monkeypatch.setattr(fleet_skills, "ROOTS", [tmp_path / "empty"])
    monkeypatch.setattr(fleet_skills, "MARKETPLACE_GLOB", tmp_path / "empty-mp")
    monkeypatch.setattr(fleet_skills, "INDEX_HINT", index)
    catalog = await fleet_skills.load_catalog()
    assert {s["name"] for s in catalog["skills"]} == {"from-hub"}
    assert catalog["skills"][0]["source"] == "hermes-hub-index"
