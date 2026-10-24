"""Authenticated tool-description editor using AstrBot's native plugin page."""

from __future__ import annotations

from astrbot.api.star import Context, Star, StarTools
from astrbot.api.web import error_response, json_response, request

from .descriptions import ConflictError, DescriptionStore, ToolCatalog
from .schema_bridge import SchemaBridge


class ToolPrompts(Star):
    def __init__(self, context: Context):
        super().__init__(context)
        directory = StarTools.get_data_dir("astrbot_plugin_tool_prompts")
        self.store = DescriptionStore(directory / "overrides.json")
        self.catalog = ToolCatalog(context.get_llm_tool_manager(), self.store)
        self.bridge = SchemaBridge(self.store)
        self.running = False
        for path, handler, methods in (
            ("tools", self.api_tools, ["GET"]),
            ("save", self.api_save, ["POST"]),
            ("reset", self.api_reset, ["POST"]),
            ("enabled", self.api_enabled, ["POST"]),
        ):
            context.register_web_api(
                "/astrbot_plugin_tool_prompts/" + path,
                handler,
                methods,
                "Tool description editor",
            )

    async def initialize(self):
        self.bridge.start()
        self.running = True

    async def api_tools(self):
        if not self.running:
            return error_response("插件未启用，请启用后刷新页面", status_code=503)
        return json_response(self.catalog.view())

    async def _update(self, *, reset=False, enabled=False):
        if not self.running:
            return error_response("插件未启用，请启用后刷新页面", status_code=503)
        try:
            payload = await request.json()
            result = (
                await self.catalog.set_active(payload)
                if enabled
                else self.catalog.update(payload, reset=reset)
            )
            return json_response(result)
        except ConflictError as exc:
            return error_response(str(exc), status_code=409)
        except (ValueError, TypeError) as exc:
            return error_response(str(exc), status_code=400)
        except Exception:
            self.logger.exception("Failed to persist tool settings")
            return error_response("设置保存失败，请检查后台日志后重试", status_code=500)

    async def api_save(self):
        return await self._update()

    async def api_reset(self):
        return await self._update(reset=True)

    async def api_enabled(self):
        return await self._update(enabled=True)

    async def terminate(self):
        self.running = False
        self.bridge.stop()
        # AstrBot retains registered routes across reloads. Retire only ours.
        apis = getattr(self.context, "registered_web_apis", None)
        if isinstance(apis, list):
            apis[:] = [
                api for api in apis if getattr(api[1], "__self__", None) is not self
            ]
