from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from astrbot.core.agent.mcp_client import MCPTool
from astrbot.core.agent.tool import FunctionTool
from astrbot.core.db.sqlite import SQLiteDatabase
from astrbot.core.star.star import star_map
from astrbot.core.utils.shared_preferences import SharedPreferences
from astrbot_plugin_tool_prompts.descriptions import ConflictError, tool_id
from mcp.types import Tool
from test_descriptions import save
from test_descriptions import setup as setup_fixture

setup = setup_fixture


@pytest.mark.anyio
async def test_native_activation_persists_and_does_not_change_descriptions(
    setup, tmp_path, monkeypatch
):
    from astrbot.core.provider import func_tool_manager

    database = SQLiteDatabase(str(tmp_path / "native-preferences.sqlite3"))
    preferences = SharedPreferences(database)
    monkeypatch.setattr(func_tool_manager, "sp", preferences)
    save(setup)
    content = setup.store.path.read_bytes()
    try:
        off = await setup.catalog.set_active(
            {"id": tool_id(setup.tool), "active": False}
        )
        assert setup.tool.active is False
        assert (
            next(item for item in off["tools"] if item["id"] == tool_id(setup.tool))[
                "active"
            ]
            is False
        )
        await preferences.close()
        preferences = SharedPreferences(database)
        monkeypatch.setattr(func_tool_manager, "sp", preferences)
        assert await preferences.get_async(
            "global", "global", "inactivated_llm_tools", []
        ) == [setup.tool.name]
        setup.tool.active = setup.tool.name not in await preferences.get_async(
            "global", "global", "inactivated_llm_tools", []
        )
        assert setup.tool.active is False
        await setup.catalog.set_active({"id": tool_id(setup.tool), "active": True})
        assert setup.tool.active is True
        assert (
            await preferences.get_async("global", "global", "inactivated_llm_tools", [])
            == []
        )
        assert setup.store.path.read_bytes() == content
        assert setup.store.revision == 1
    finally:
        await preferences.close()
        await database.engine.dispose()


@pytest.mark.anyio
async def test_failed_native_persistence_restores_active_state(setup, monkeypatch):
    from astrbot.core.provider import func_tool_manager

    monkeypatch.setattr(
        func_tool_manager,
        "sp",
        SimpleNamespace(
            get_async=AsyncMock(return_value=[]),
            put_async=AsyncMock(side_effect=OSError("isolated disk failure")),
        ),
    )
    with pytest.raises(OSError):
        await setup.catalog.set_active({"id": tool_id(setup.tool), "active": False})
    assert setup.tool.active is True


@pytest.mark.anyio
async def test_builtin_disabled_plugin_and_ambiguous_names_are_protected(setup):
    builtin = setup.manager.iter_builtin_tools()[0]
    assert not setup.catalog.describe(builtin)["can_toggle"]
    with pytest.raises(ValueError, match="内置"):
        await setup.catalog.set_active({"id": tool_id(builtin), "active": False})
    setup.tool.active = False
    star_map["test.plugin"].activated = False
    with pytest.raises(ValueError, match="插件已停用"):
        await setup.catalog.set_active({"id": tool_id(setup.tool), "active": True})
    star_map["test.plugin"].activated = True
    duplicate = FunctionTool(
        name=setup.tool.name,
        description="another source",
        parameters={},
        handler_module_path="another.plugin",
    )
    setup.manager.func_list.append(duplicate)
    with pytest.raises(ValueError, match="同名"):
        await setup.catalog.set_active({"id": tool_id(setup.tool), "active": True})
    assert duplicate.active is True


@pytest.mark.anyio
async def test_mcp_activation_uses_native_settings_without_connecting(
    setup, monkeypatch
):
    from astrbot.core.provider import func_tool_manager

    native = SimpleNamespace(
        get_async=AsyncMock(return_value=[]), put_async=AsyncMock()
    )
    monkeypatch.setattr(func_tool_manager, "sp", native)
    client = SimpleNamespace(call_tool_with_reconnect=AsyncMock())
    tool = MCPTool(
        Tool(
            name="example.lookup",
            description="MCP tool",
            inputSchema={"type": "object", "properties": {}},
        ),
        client,
        "test-server",
    )
    setup.manager.func_list.append(tool)
    await setup.catalog.set_active({"id": tool_id(tool), "active": False})
    assert tool.active is False
    native.put_async.assert_awaited_once_with(
        "global", "global", "inactivated_llm_tools", [tool.name]
    )
    assert client.call_tool_with_reconnect.await_count == 0


@pytest.mark.anyio
async def test_activation_requires_boolean_and_existing_tool(setup):
    with pytest.raises(ValueError):
        await setup.catalog.set_active({"id": tool_id(setup.tool), "active": "false"})
    with pytest.raises(ConflictError):
        await setup.catalog.set_active({"id": "unloaded", "active": False})
    assert setup.tool.active is True


def test_catalog_does_not_stop_at_one_hundred_and_keeps_both_meme_sources(setup):
    tools = [
        FunctionTool(name=f"bulk_{index}", description="", parameters={})
        for index in range(130)
    ]
    for name in ("meme_search", "meme_view", "meme_edit", "meme_disable", "meme_send"):
        tools.append(
            FunctionTool(
                name=name,
                description="",
                parameters={},
                handler_module_path="new.library",
            )
        )
    tools.append(
        FunctionTool(
            name="search_memes",
            description="",
            parameters={},
            handler_module_path="old.manager",
            active=False,
        )
    )
    setup.manager.func_list = tools
    view = setup.catalog.view()
    assert view["total"] == len(view["tools"]) > 136
    assert len([item for item in view["tools"] if item["source"] == "new.library"]) == 5
    assert len([item for item in view["tools"] if item["source"] == "old.manager"]) == 1
    assert next(item for item in view["tools"] if item["name"] == "bulk_129")
