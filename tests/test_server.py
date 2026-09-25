from __future__ import annotations

import importlib

import pytest

from edupage_mcp import server
from edupage_mcp.client import EdupageClient
from edupage_mcp.service import EdupageService
from conftest import FakeEdupage

READ_TOOLS = {
    "get_account", "get_briefing", "get_timetable", "get_homework", "get_messages",
    "get_notifications", "get_grades", "get_absences", "get_events", "get_substitutions",
    "get_meals", "get_school_info", "list_teachers", "complete_login",
}
WRITE_TOOLS = {"send_message", "order_meal"}


@pytest.fixture(autouse=True)
def reset_service(monkeypatch):
    monkeypatch.setattr(server, "_service", None)


def _payload(result):
    assert result.structured_content is not None
    return result.structured_content


@pytest.mark.anyio
async def test_all_tools_are_registered():
    tools = await server.mcp.list_tools()
    assert {t.name for t in tools} == READ_TOOLS | WRITE_TOOLS


@pytest.mark.anyio
async def test_read_tools_are_annotated_read_only():
    for tool in await server.mcp.list_tools():
        if tool.name in READ_TOOLS - {"complete_login"}:
            assert tool.annotations.read_only_hint is True, tool.name
        if tool.name in WRITE_TOOLS:
            assert tool.annotations.read_only_hint is False, tool.name


@pytest.mark.anyio
async def test_timetable_schema():
    tool = next(t for t in await server.mcp.list_tools() if t.name == "get_timetable")
    assert set(tool.input_schema["properties"]) == {"student", "date", "days"}
    assert not tool.input_schema.get("required")


@pytest.mark.anyio
async def test_missing_credentials_surface_as_a_tool_error(monkeypatch):
    monkeypatch.delenv("EDUPAGE_USERNAME", raising=False)
    monkeypatch.delenv("EDUPAGE_PASSWORD", raising=False)
    payload = _payload(await server.mcp.call_tool("get_account", {}))
    assert payload["status"] == "error"
    assert "EDUPAGE_USERNAME" in payload["error"]


@pytest.mark.anyio
async def test_a_tool_round_trips(monkeypatch, config):
    service = EdupageService(config, EdupageClient(config, factory=FakeEdupage))
    monkeypatch.setattr(server, "_get_service", lambda: service)

    payload = _payload(await server.mcp.call_tool("get_account", {}))
    assert payload["status"] == "success"
    assert payload["account_type"] == "parent"
    assert [s["name"] for s in payload["students"]] == ["Jana Testová"]

    payload = _payload(await server.mcp.call_tool("get_timetable", {"student": "nobody"}))
    assert payload["status"] == "error" and "Jana Testová" in payload["error"]


@pytest.mark.anyio
async def test_unexpected_errors_are_reported_not_raised(monkeypatch, config):
    service = EdupageService(config, EdupageClient(config, factory=FakeEdupage))
    monkeypatch.setattr(server, "_get_service", lambda: service)
    FakeEdupage.routes = {"currenttt.js": {"r": {}}}  # no ttitems: parse fails twice
    payload = _payload(await server.mcp.call_tool("get_timetable", {}))
    assert payload["status"] == "error"
    assert "RuntimeError" in payload["error"]


@pytest.mark.anyio
async def test_read_only_mode_hides_the_write_tools(monkeypatch):
    monkeypatch.setenv("EDUPAGE_READ_ONLY", "true")
    try:
        importlib.reload(server)
        assert {t.name for t in await server.mcp.list_tools()} == READ_TOOLS
    finally:
        monkeypatch.delenv("EDUPAGE_READ_ONLY")
        importlib.reload(server)
