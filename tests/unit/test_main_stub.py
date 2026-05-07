"""Test the Phase 10 placeholder entrypoint in src/fleet/__main__.py."""

import io
import runpy
import sys

import pytest


def test_main_stub_prints_message_and_exits_zero(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured = io.StringIO()
    monkeypatch.setattr(sys, "stderr", captured)
    with pytest.raises(SystemExit) as exc_info:
        runpy.run_module("fleet", run_name="__main__")
    assert exc_info.value.code == 0
    assert "not yet implemented" in captured.getvalue()
