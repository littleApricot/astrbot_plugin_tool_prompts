import copy
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import mcp
import pytest
from astrbot.core.agent.mcp_client import MCPTool
from astrbot.core.agent.tool import FunctionTool, ToolSet
from astrbot.core.astr_agent_tool_exec import FunctionToolExecutor
from astrbot.core.provider.func_tool_manager import FunctionToolManager
from astrbot.core.star.star import StarMetadata, star_map
from astrbot_plugin_tool_prompts.descriptions import (
    ConflictError,
    DescriptionStore,
    ToolCatalog,
    tool_id,
)
from astrbot_plugin_tool_prompts.schema_bridge import SchemaBridge


@pytest.fixture
def setup(tmp_path, monkeypatch):
    monkeypatch.setitem(
        star_map,
        "test.plugin",
        StarMetadata(name="test-plugin", display_name="测试插件"),
    )
    tool = FunctionTool(
        name="search_meme",
        description="原始工具说明",
        handler_module_path="test.plugin",
        parameters={
            "type": "object",
            "required": ["query"],
            "additionalProperties": False,
            "properties": {
                "query": {
                    "type": "string",
                    "description": "原始查询说明",
                    "enum": ["a", "b"],
                },
                "limit": {"type": "integer", "minimum": 1, "default": 4},
                "nested/~": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {"x": {"type": "string"}},
                    },
                },
            },
        },
    )
    manager = object.__new__(FunctionToolManager)
    manager.func_list = [tool]
    manager.builtin_func_list = {}
    store = DescriptionStore(tmp_path / "overrides.json")
    catalog = ToolCatalog(manager, store)
    bridge = SchemaBridge(store)
    bridge.start()
    yield SimpleNamespace(
        tool=tool, manager=manager, store=store, catalog=catalog, bridge=bridge
    )
    bridge.stop()


def save(stack, description="新工具说明", parameters=None):
    current = stack.catalog.describe(stack.tool)
    return stack.catalog.update(
        {
            "id": current["id"],
            "revision": stack.store.revision,
            "definition_digest": current["definition_digest"],
            "description": description,
            "parameters": parameters
            if parameters is not None
            else {"/properties/query": "新查询说明"},
        }
    )


@pytest.mark.parametrize("format", ["openai", "anthropic", "google"])
def test_native_schema_override_preserves_contract_and_original_tools(setup, format):
    stack = setup
    original = copy.deepcopy(stack.tool.parameters)
    native = ToolSet([stack.tool])
    save(
        stack,
        parameters={
            "/properties/query": "新查询说明",
            "/properties/nested~1~0/items/properties/x": "嵌套说明",
        },
    )
    serialized = getattr(native, format + "_schema")()
    if format == "openai":
        item = serialized[0]["function"]
    elif format == "anthropic":
        item = serialized[0]
    else:
        item = serialized["function_declarations"][0]
    assert item["description"] == "新工具说明"
    params = item["input_schema" if format == "anthropic" else "parameters"]
    assert params["required"] == ["query"]
    assert params["properties"]["query"]["enum"] == ["a", "b"]
    assert params["properties"]["query"]["description"] == "新查询说明"
    assert (
        params["properties"]["nested/~"]["items"]["properties"]["x"]["description"]
        == "嵌套说明"
    )
    assert stack.tool.description == "原始工具说明"
    assert stack.tool.parameters == original
    assert native.tools[0] is stack.tool


def test_native_light_and_parameter_modes_keep_separate_fields(setup):
    save(setup, parameters={"": "参数整体说明", "/properties/query": "新参数说明"})
    native = setup.manager.get_full_tool_set()
    light = native.get_light_tool_set()
    parameters = native.get_param_only_tool_set()
    first = light.openai_schema()[0]["function"]
    assert first["description"] == "新工具说明"
    assert first["parameters"] == {"type": "object", "properties": {}}
    second = parameters.openai_schema()[0]["function"]
    assert "description" not in second
    assert second["parameters"]["properties"]["query"]["description"] == "新参数说明"
    assert second["parameters"]["description"] == "参数整体说明"
    assert native.get_tool("search_meme").handler is None


def test_same_name_sources_and_mcp_reconnect_do_not_mix(setup):
    client = SimpleNamespace(
        call_tool_with_reconnect=AsyncMock(
            return_value=mcp.types.CallToolResult(content=[])
        )
    )
    spec = mcp.types.Tool(
        name="search.meme",
        description="MCP 默认说明",
        inputSchema=setup.tool.parameters,
    )
    first = MCPTool(spec, client, "server-one")
    second = MCPTool(spec, client, "server-two")
    setup.manager.func_list = [setup.tool, first, second]
    setup.store.save(
        tool_id(first), {"description": "server-one 自定义"}, setup.store.revision
    )
    native = ToolSet([MCPTool(spec, client, "server-one")])
    assert native.openai_schema()[0]["function"]["description"] == "server-one 自定义"
    assert (
        ToolSet([second]).openai_schema()[0]["function"]["description"]
        == "MCP 默认说明"
    )
    assert (
        ToolSet([setup.tool]).openai_schema()[0]["function"]["description"]
        == "原始工具说明"
    )
    assert (
        len(
            {
                item["id"]
                for item in setup.catalog.view()["tools"]
                if item["name"] == "search_meme"
            }
        )
        == 3
    )
    assert client.call_tool_with_reconnect.await_count == 0


@pytest.mark.anyio
async def test_mcp_execution_keeps_original_name_client_and_arguments(setup):
    client = SimpleNamespace(
        call_tool_with_reconnect=AsyncMock(
            return_value=mcp.types.CallToolResult(content=[])
        )
    )
    spec = mcp.types.Tool(
        name="search.meme", description="MCP 默认", inputSchema=setup.tool.parameters
    )
    native = MCPTool(spec, client, "mcp-server")
    setup.store.save(tool_id(native), {"description": "自定义 MCP"}, 0)
    toolset = ToolSet([native])
    assert toolset.openai_schema()[0]["function"]["description"] == "自定义 MCP"
    await native.call(SimpleNamespace(tool_call_timeout=20), query="a")
    assert (
        client.call_tool_with_reconnect.await_args.kwargs["tool_name"] == "search.meme"
    )
    assert client.call_tool_with_reconnect.await_args.kwargs["arguments"] == {
        "query": "a"
    }


def test_builtin_description_is_editable_without_enabling_or_replacing_tool(setup):
    builtin = setup.manager.iter_builtin_tools()[0]
    original = builtin.description
    setup.store.save(tool_id(builtin), {"description": "自定义内置工具"}, 0)
    assert (
        ToolSet([builtin]).openai_schema()[0]["function"]["description"]
        == "自定义内置工具"
    )
    assert setup.manager.get_builtin_tool(builtin.name) is builtin
    assert builtin.description == original
    described = setup.catalog.describe(builtin)
    assert described["source_kind"] == "builtin"
    assert described["overridden"] is True


@pytest.mark.anyio
async def test_native_tool_executor_retains_permission_guard_and_handler(
    setup, monkeypatch
):
    from astrbot.core.provider import func_tool_manager

    monkeypatch.setattr(
        func_tool_manager.sp,
        "global_get",
        AsyncMock(return_value={"_default": {"search_meme": "admin"}}),
    )
    handler = AsyncMock(return_value="handler-ran")
    setup.tool.handler = handler
    save(setup)
    toolset = setup.manager.get_full_tool_set()
    guarded = toolset.get_tool("search_meme")
    assert toolset.openai_schema()[0]["function"]["description"] == "新工具说明"
    event = SimpleNamespace(is_admin=lambda: False, get_sender_id=lambda: "test-user")
    context = SimpleNamespace(
        context=SimpleNamespace(event=event), tool_call_timeout=20
    )
    results = [
        item async for item in FunctionToolExecutor.execute(guarded, context, query="a")
    ]
    assert "Permission denied" in results[0].content[0].text
    assert handler.await_count == 0
    event.is_admin = lambda: True
    results = [
        item async for item in FunctionToolExecutor.execute(guarded, context, query="a")
    ]
    assert results[0].content[0].text == "handler-ran"
    handler.assert_awaited_once_with(event, query="a")


def test_saved_overrides_survive_restart_and_reset_uses_updated_defaults(setup):
    save(setup)
    restored = DescriptionStore(setup.store.path)
    assert restored.overrides == setup.store.overrides
    setup.bridge.stop()
    next_bridge = SchemaBridge(restored)
    next_bridge.start()
    try:
        reloaded = copy.copy(setup.tool)
        reloaded.description = "升级后的默认说明"
        setup.manager.func_list = [reloaded]
        assert (
            ToolSet([reloaded]).openai_schema()[0]["function"]["description"]
            == "新工具说明"
        )
        catalog = ToolCatalog(setup.manager, restored)
        item = catalog.describe(reloaded)
        catalog.update(
            {
                "id": item["id"],
                "revision": restored.revision,
                "definition_digest": item["definition_digest"],
            },
            reset=True,
        )
        assert (
            ToolSet([reloaded]).openai_schema()[0]["function"]["description"]
            == "升级后的默认说明"
        )
    finally:
        next_bridge.stop()


def test_removed_parameters_are_ignored_and_cleaned_on_save(setup):
    save(setup)
    setup.tool.parameters = {
        "type": "object",
        "properties": {"new": {"type": "string"}},
    }
    current = setup.catalog.describe(setup.tool)
    assert current["stale_parameters"] == ["/properties/query"]
    schema = ToolSet([setup.tool]).openai_schema()[0]["function"]["parameters"]
    assert "query" not in schema["properties"]
    save(setup, parameters={})
    assert setup.catalog.describe(setup.tool)["stale_parameters"] == []


def test_stale_editor_and_schema_changes_are_rejected(setup):
    item = setup.catalog.describe(setup.tool)
    payload = {
        "id": item["id"],
        "revision": 0,
        "definition_digest": item["definition_digest"],
        "description": "edited",
        "parameters": {},
    }
    save(setup)
    with pytest.raises(ConflictError):
        setup.catalog.update(payload)
    payload["revision"] = 1
    setup.tool.description = "插件更新"
    with pytest.raises(ConflictError):
        setup.catalog.update(payload)


@pytest.mark.parametrize(
    "extra",
    [
        {"handler": "code"},
        {"active": True},
        {"permission": "member"},
        {"parameters": {"/required": "x"}},
        {"description": 123},
    ],
)
def test_editor_rejects_execution_fields_and_invalid_nodes(setup, extra):
    item = setup.catalog.describe(setup.tool)
    payload = {
        "id": item["id"],
        "revision": 0,
        "definition_digest": item["definition_digest"],
        "description": "edited",
        "parameters": {},
        **extra,
    }
    with pytest.raises(ValueError):
        setup.catalog.update(payload)
    assert setup.store.revision == 0


def test_atomic_save_failure_keeps_memory_and_file_consistent(setup, monkeypatch):
    save(setup)
    before = setup.store.path.read_text()
    import astrbot_plugin_tool_prompts.descriptions as module

    monkeypatch.setattr(
        module.os, "replace", lambda *_: (_ for _ in ()).throw(OSError("disk failure"))
    )
    with pytest.raises(OSError):
        save(setup, "second")
    assert setup.store.path.read_text() == before
    assert setup.store.revision == 1
    assert setup.store.overrides[tool_id(setup.tool)]["description"] == "新工具说明"
    assert not list(setup.store.path.parent.glob(".tool-prompts-*"))


def test_reload_and_unload_do_not_accumulate_active_hooks(setup):
    original = setup.bridge.originals["openai_schema"]
    save(setup)
    second = SchemaBridge(setup.store)
    second.start()
    setup.bridge.stop()
    assert (
        ToolSet([setup.tool]).openai_schema()[0]["function"]["description"]
        == "新工具说明"
    )
    second.stop()
    assert ToolSet.openai_schema is original
    assert (
        ToolSet([setup.tool]).openai_schema()[0]["function"]["description"]
        == "原始工具说明"
    )


def test_unload_preserves_later_third_party_wrapper(setup, monkeypatch):
    save(setup)
    wrapped = ToolSet.openai_schema
    calls = []

    def later(self, *args, **kwargs):
        calls.append(True)
        return wrapped(self, *args, **kwargs)

    monkeypatch.setattr(ToolSet, "openai_schema", later)
    setup.bridge.stop()
    assert ToolSet.openai_schema is later
    assert (
        ToolSet([setup.tool]).openai_schema()[0]["function"]["description"]
        == "原始工具说明"
    )
    assert calls == [True]


def test_invalid_saved_file_is_never_silently_overwritten(tmp_path):
    path = tmp_path / "overrides.json"
    path.write_text('{"version":99,"overrides":{}}')
    with pytest.raises(ValueError):
        DescriptionStore(path)
    assert json.loads(path.read_text())["version"] == 99
