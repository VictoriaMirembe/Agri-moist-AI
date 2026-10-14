# The "AI tools (MCP)" screen and GET /api/mcp start both MCP servers over stdio and
# list what they report over the protocol, so these tests also prove the servers start.

AGRIMOIST_TOOLS = {
    "record_observation",
    "get_field_records",
    "get_plot_history",
    "get_crop_calendar",
    "get_weather",
    "get_pest_reports",
    "draft_advice",
    "flag_for_visit",
    "schedule_followup",
}


def test_api_mcp_lists_both_servers_and_all_tools(client):
    data = client.get("/api/mcp").json()
    assert data["error"] is None
    servers = {s["name"]: s for s in data["servers"]}
    assert set(servers) == {"agrimoist", "mcp-time"}
    agrimoist = {t["name"]: t for t in servers["agrimoist"]["tools"]}
    assert set(agrimoist) == AGRIMOIST_TOOLS
    assert agrimoist["draft_advice"]["writes"] is True
    assert agrimoist["get_weather"]["writes"] is False
    assert "latitude" in agrimoist["get_weather"]["inputs"]
    assert "get_current_time" in {t["name"] for t in servers["mcp-time"]["tools"]}


def test_mcp_page_renders_and_is_in_the_sidebar(client):
    page = client.get("/mcp")
    assert page.status_code == 200
    assert "AI tools (MCP)" in page.text
    assert "draft_advice" in page.text and "get_current_time" in page.text
    assert 'href="/mcp"' in client.get("/log").text


def test_mcp_page_shows_ai_services_but_never_keys(client, monkeypatch):
    from backend.core.config import settings

    monkeypatch.setattr(settings, "sunbird_api_key", "sb-secret-123")
    data = client.get("/api/mcp").json()
    services = {s["name"]: s for s in data["ai_services"]}
    assert services["Sunbird AI (sunflower-9b)"]["linked"] is True
    assert not services[f"Groq ({settings.groq_model})"]["linked"]  # tests run without a key
    assert "sb-secret-123" not in client.get("/mcp").text
