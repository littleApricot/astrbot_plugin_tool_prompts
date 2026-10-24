"""Real plugin handlers and native schemas with synthetic tools and no network."""

import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from astrbot.core.agent.tool import FunctionTool
from astrbot.core.provider.func_tool_manager import FunctionToolManager
from astrbot.core.star.star import StarMetadata, star_map
from fastapi import FastAPI, Request

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from astrbot_plugin_tool_prompts.main import ToolPrompts

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
registered = []


def register(path, handler, methods, description):
    registered[:] = [item for item in registered if item[0] != path]
    registered.append((path, handler, methods, description))


context = SimpleNamespace(
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


@app.api_route("/api/{name}", methods=["GET", "POST"])
async def api(name: str, request: Request):
    global plugin
    if not plugin.running:
        await plugin.initialize()
    if name == "schema":
        return {
            "schema": manager.get_full_tool_set().openai_schema(),
            "original": manager.func_list[0].description,
        }
    if name == "restart":
        await plugin.terminate()
        plugin = make_plugin()
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
