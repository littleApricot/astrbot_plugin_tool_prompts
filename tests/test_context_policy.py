"""Check reload/retirement through the actual management plugin lifecycle."""

from types import SimpleNamespace

import pytest
from astrbot.core.provider.func_tool_manager import FunctionToolManager

from astrbot_plugin_tool_prompts.main import ToolPrompts


@pytest.mark.asyncio
async def test_existing_identity_data_and_routes_survive_reload(tmp_path, monkeypatch):
    manager = object.__new__(FunctionToolManager)
    manager.func_list, manager.builtin_func_list = [], {}
    routes = []
    context = SimpleNamespace(
        get_llm_tool_manager=lambda: manager,
        astrbot_config_mgr=SimpleNamespace(),
        registered_web_apis=routes,
        register_web_api=lambda path, handler, methods, label: routes.append(
            (path, handler, methods, label)
        ),
    )
    paths = []

    def data(name):
        paths.append(name)
        return tmp_path

    monkeypatch.setattr("astrbot_plugin_tool_prompts.main.StarTools.get_data_dir", data)
    first = ToolPrompts(context)
    await first.initialize()
    first.store.save("test-tool", {"description": "保留的自定义说明"}, 0)
    assert first.model_context_policy() is first.policy
    await first.terminate()
    assert first.model_context_policy() is None and routes == []
    second = ToolPrompts(context)
    try:
        await second.initialize()
        assert second.store.overrides["test-tool"]["description"] == "保留的自定义说明"
        assert paths == ["astrbot_plugin_tool_prompts"] * 2
        assert len(routes) == 5 and all(route[3] == "工具管理" for route in routes)
        assert all(
            route[0].startswith("/astrbot_plugin_tool_prompts/") for route in routes
        )
    finally:
        await second.terminate()
