import copy
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from astrbot.core.astr_main_agent import _apply_web_search_tools
from astrbot.core.astrbot_config_mgr import AstrBotConfigManager
from astrbot.core.config.astrbot_config import AstrBotConfig
from astrbot.core.config.default import DEFAULT_CONFIG
from astrbot.core.provider.entites import ProviderRequest
from astrbot.core.tools.registry import get_builtin_tool_config_rule
from astrbot.dashboard.services.config_service import ConfigProfileService
from astrbot_plugin_tool_prompts.descriptions import ConflictError
from astrbot_plugin_tool_prompts.native_config import (
    FIELDS,
    NativeConfigs,
    revision,
    value_at,
)
from test_descriptions import setup as setup_fixture

setup = setup_fixture


@pytest.fixture
def native(setup, tmp_path):
    configs = [
        AstrBotConfig(
            str(tmp_path / f"{name}.json"), default_config=copy.deepcopy(DEFAULT_CONFIG)
        )
        for name in ("default", "second")
    ]
    manager = AstrBotConfigManager(configs[0], None, None)
    manager.confs["second"] = configs[1]
    manager.abconf_data = {"second": {"name": "Second", "path": "second.json"}}
    lifecycle = SimpleNamespace(
        astrbot_config_mgr=manager, reload_pipeline_scheduler=AsyncMock()
    )
    return SimpleNamespace(
        editor=NativeConfigs(manager),
        service=ConfigProfileService(lifecycle, runtime={}),
        manager=manager,
        lifecycle=lifecycle,
        catalog=setup.catalog,
    )


@pytest.mark.anyio
async def test_original_profile_is_saved_reloaded_and_drives_native_tool_setup(
    native, setup
):
    config = native.manager.confs["default"]
    config["provider_settings"]["web_search"] = False
    config["provider_settings"]["tavily_api_key"] = ["isolated-test-placeholder"]
    config.save_config()
    context = SimpleNamespace(
        get_config=lambda **_: config, get_llm_tool_manager=lambda: setup.manager
    )
    event = SimpleNamespace(unified_msg_origin="isolated")

    async def request_tools():
        req = ProviderRequest()
        await _apply_web_search_tools(event, req, context)
        return (
            [] if req.func_tool is None else [tool.name for tool in req.func_tool.tools]
        )

    assert await request_tools() == []
    view = native.editor.view(native.catalog.view(), "default")
    assert {item["id"] for item in view["profiles"]} == {"default", "second"}
    tool = next(t for t in view["tools"] if t["name"] == "web_search_tavily")
    assert not tool["active"]
    assert "tavily_extract_web_page" in tool["native"]["linked_tools"]
    assert "isolated-test-placeholder" not in json.dumps(view)
    await native.editor.update(
        {
            "revision": view["config_revision"],
            "values": {
                "provider_settings.web_search": True,
                "provider_settings.websearch_provider": "tavily",
            },
        },
        "default",
        native.service,
    )
    native.lifecycle.reload_pipeline_scheduler.assert_awaited_once_with("default")
    assert set(await request_tools()) == {
        "web_search_tavily",
        "tavily_extract_web_page",
    }
    saved = native.service.get_profile("default")["config"]
    assert saved is config
    assert saved["provider_settings"]["web_search"] is True
    assert saved["provider_settings"]["tavily_api_key"] == ["isolated-test-placeholder"]
    on = native.editor.view(native.catalog.view(), "default")
    request_names = await request_tools()
    assert all(t["active"] for t in on["tools"] if t["name"] in request_names)
    # A change in the original configuration page is visible here immediately.
    original_page = copy.deepcopy(dict(config))
    original_page["provider_settings"]["websearch_provider"] = "brave"
    await native.service.update_profile("default", original_page)
    assert await request_tools() == ["web_search_brave"]
    changed = native.editor.view(native.catalog.view(), "default")
    assert not next(t for t in changed["tools"] if t["name"] == "web_search_tavily")[
        "active"
    ]
    assert next(t for t in changed["tools"] if t["name"] == "web_search_brave")[
        "active"
    ]
    with pytest.raises(ConflictError):
        await native.editor.update(
            {
                "revision": on["config_revision"],
                "values": {"provider_settings.web_search": False},
            },
            "default",
            native.service,
        )
    await native.editor.update(
        {
            "revision": changed["config_revision"],
            "values": {"provider_settings.web_search": False},
        },
        "default",
        native.service,
    )
    assert await request_tools() == []
    from pathlib import Path

    disk = json.loads(Path(config.config_path).read_text(encoding="utf-8-sig"))
    assert disk["provider_settings"]["web_search"] is False
    assert disk["provider_settings"]["websearch_provider"] == "brave"
    assert not setup.store.overrides


@pytest.mark.anyio
async def test_profile_scope_and_unrelated_native_changes_are_preserved(native):
    primary = copy.deepcopy(dict(native.manager.confs["default"]))
    second = native.manager.confs["second"]
    digest = revision(second)
    # An unrelated native-page update between read/save must survive.
    second["admins_id"] = ["isolated-owner"]
    second.save_config()
    await native.editor.update(
        {
            "revision": digest,
            "values": {"provider_settings.proactive_capability.add_cron_tools": False},
        },
        "second",
        native.service,
    )
    assert dict(native.manager.confs["default"]) == primary
    assert second["admins_id"] == ["isolated-owner"]
    assert (
        value_at(second, "provider_settings.proactive_capability.add_cron_tools")
        is False
    )
    native.lifecycle.reload_pipeline_scheduler.assert_awaited_once_with("second")


@pytest.mark.anyio
@pytest.mark.parametrize(
    "values",
    [
        {"admins_id": []},
        {"platform": []},
        {"provider_settings.web_search": 1},
        {"provider_settings.websearch_provider": "unknown"},
        {},
    ],
)
async def test_native_write_allowlist_and_typed_values(native, values):
    before = copy.deepcopy(dict(native.manager.confs["default"]))
    with pytest.raises(ValueError):
        await native.editor.update(
            {"revision": revision(before), "values": values}, "default", native.service
        )
    assert dict(native.manager.confs["default"]) == before
    native.lifecycle.reload_pipeline_scheduler.assert_not_awaited()


def test_all_builtin_conditions_are_covered_and_platform_tools_explain_their_source(
    native,
):
    view = native.editor.view(native.catalog.view(), "default")
    for tool in view["tools"]:
        if tool["source_kind"] != "builtin":
            continue
        rule = get_builtin_tool_config_rule(tool["name"])
        assert rule is not None
        assert all(condition.key in FIELDS for condition in rule.conditions)
        if tool["name"] == "send_message_to_user":
            assert not tool["native"]["fields"]
            assert "平台能力" in tool["native"]["note"]
        else:
            assert tool["native"]["fields"]
