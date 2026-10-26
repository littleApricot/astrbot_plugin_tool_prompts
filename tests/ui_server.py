"""Real plugin handlers and native schemas with synthetic tools and no network."""

import copy
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from astrbot.core.agent.tool import FunctionTool
from astrbot.core.astrbot_config_mgr import AstrBotConfigManager
from astrbot.core.config.astrbot_config import AstrBotConfig
from astrbot.core.config.default import DEFAULT_CONFIG
from astrbot.core.db.sqlite import SQLiteDatabase
from astrbot.core.provider import func_tool_manager
from astrbot.core.provider.func_tool_manager import FunctionToolManager
from astrbot.core.star.star import StarMetadata, star_map
from astrbot.core.utils.shared_preferences import SharedPreferences
from astrbot.dashboard.services.config_service import ConfigProfileService
from fastapi import FastAPI, Request

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from astrbot_plugin_tool_prompts.main import ToolPrompts
from astrbot_plugin_tool_prompts.native_config import FIELDS, value_at

temporary = tempfile.TemporaryDirectory(prefix="tool-prompts-ui-")
root = Path(temporary.name)
manager = object.__new__(FunctionToolManager)
manager.builtin_func_list = {}
manager.func_list = [
    FunctionTool(
        name="meme_search",
        description="按语义查找表情包。",
        handler_module_path="fixture.memes",
        parameters={
            "type": "object",
            "required": ["query"],
            "properties": {
                "query": {"type": "string", "description": "想表达的情绪或场景。"},
                "limit": {"type": "integer", "minimum": 1, "maximum": 20, "default": 5},
            },
        },
    ),
    FunctionTool(
        name="meme_send",
        description="<script>window.injected=true</script>",
        handler_module_path="fixture.memes",
        parameters={"type": "object", "properties": {}},
        active=False,
    ),
]
star_map["fixture.memes"] = StarMetadata(
    name="fixture-memes", display_name="表情图库（测试）"
)
for name in ("meme_view", "meme_edit", "meme_disable"):
    manager.func_list.append(
        FunctionTool(
            name=name,
            description="测试图库工具",
            parameters={"type": "object", "properties": {}},
            handler_module_path="fixture.memes",
        )
    )
manager.func_list.append(
    FunctionTool(
        name="search_memes",
        description="旧插件的独立工具",
        parameters={},
        handler_module_path="fixture.old",
        active=False,
    )
)
star_map["fixture.old"] = StarMetadata(
    name="old-memes", display_name="旧表情包管理器（测试）"
)
manager.func_list.extend(
    FunctionTool(
        name=f"bulk_{index:03d}",
        description="数量检查",
        parameters={},
        handler_module_path="fixture.bulk",
    )
    for index in range(125)
)
star_map["fixture.bulk"] = StarMetadata(
    name="bulk-fixture", display_name="数量检查（测试）"
)
preference_db = SQLiteDatabase(str(root / "native-preferences.sqlite3"))
preferences = SharedPreferences(preference_db)
func_tool_manager.sp = preferences
preferences_ready = False
conf = AstrBotConfig(
    str(root / "default.json"), default_config=copy.deepcopy(DEFAULT_CONFIG)
)
other = AstrBotConfig(
    str(root / "second.json"), default_config=copy.deepcopy(DEFAULT_CONFIG)
)
conf["provider_settings"]["web_search"] = True
conf["provider_settings"]["websearch_provider"] = "tavily"
conf.save_config()
config_manager = AstrBotConfigManager(conf, None, preferences)
config_manager.confs["second"] = other
config_manager.abconf_data = {"second": {"name": "第二配置", "path": "second.json"}}
lifecycle = SimpleNamespace(
    astrbot_config_mgr=config_manager, reload_pipeline_scheduler=AsyncMock()
)
config_service = ConfigProfileService(lifecycle, runtime={})
try:
    from astrbot_plugin_lcm.main import LCM
except ModuleNotFoundError:
    LCM = None
if LCM is not None:
    # Use the actual LCM declarations, with no worker, history or API connection.
    lcm_owner = object.__new__(LCM)
    manager.func_list.extend(lcm_owner.history_tools())
    star_map[LCM.__module__] = StarMetadata(
        name="astrbot_plugin_lcm", display_name="LCM 上下文与历史"
    )
registered = []


def register(path, handler, methods, description):
    registered[:] = [item for item in registered if item[0] != path]
    registered.append((path, handler, methods, description))


context = SimpleNamespace(
    astrbot_config_mgr=config_manager,
    get_llm_tool_manager=lambda: manager,
    register_web_api=register,
    registered_web_apis=registered,
)


def make_plugin():
    with patch(
        "astrbot_plugin_tool_prompts.main.StarTools.get_data_dir", return_value=root
    ):
        return ToolPrompts(context)


plugin = make_plugin()
app = FastAPI()
app.state.services = SimpleNamespace(config_profiles=config_service)


@app.api_route("/api/{name}", methods=["GET", "POST"])
async def api(name: str, request: Request):
    global plugin, preferences, preferences_ready
    if not preferences_ready:
        await preferences.put_async(
            "global", "global", "inactivated_llm_tools", ["meme_send", "search_memes"]
        )
        preferences_ready = True
    if not plugin.running:
        await plugin.initialize()
    if name == "schema":
        return {
            "schema": manager.get_full_tool_set().openai_schema(),
            "original": manager.func_list[0].description,
        }
    if name == "native_original":
        config = config_service.get_profile(
            request.query_params.get("profile_id", "default")
        )["config"]
        return {
            "values": {key: value_at(config, key) for key in FIELDS},
            "reloads": lifecycle.reload_pipeline_scheduler.await_count,
        }
    if name == "native_original_change":
        config = copy.deepcopy(dict(config_manager.confs["default"]))
        config["provider_settings"]["websearch_provider"] = "brave"
        await config_service.update_profile("default", config)
        return {"changed": True}
    if name == "restart":
        await plugin.terminate()
        await preferences.close()
        preferences = SharedPreferences(preference_db)
        func_tool_manager.sp = preferences
        disabled = await preferences.get_async(
            "global", "global", "inactivated_llm_tools", []
        )
        for tool in manager.func_list:
            tool.active = tool.name not in disabled
        plugin = make_plugin()
        for config in config_manager.confs.values():
            config.clear()
            import json

            config.update(
                json.loads(Path(config.config_path).read_text(encoding="utf-8-sig"))
            )
        await plugin.initialize()
        return {"restarted": True}
    if name == "definition_change":
        manager.func_list[0].description = "插件升级后的默认说明。"
        return {"changed": True}
    # The same native extension dispatcher used by the authenticated dashboard.
    from astrbot.dashboard.api.plugins import _call_plugin_extension

    app.state.core_lifecycle = SimpleNamespace(star_context=context)
    return await _call_plugin_extension(
        "astrbot_plugin_tool_prompts/" + name, request, "local-test"
    )
