"""Persistent description overrides; tool contracts and handlers stay unchanged."""

from __future__ import annotations

import copy
import hashlib
import json
import os
import tempfile
from pathlib import Path

from astrbot.core.agent.mcp_client import MCPTool
from astrbot.core.star.star import star_map
from astrbot.core.tools.registry import get_builtin_tool_name


class ConflictError(ValueError):
    """An editor used a stale definition or revision."""


def schema_nodes(schema, path="", label="参数整体", required=False):
    """Yield editable JSON Schema nodes, never dictionaries containing data."""
    if not isinstance(schema, dict):
        return
    yield path, label, schema, required
    for keyword in (
        "properties",
        "$defs",
        "definitions",
        "patternProperties",
        "dependentSchemas",
    ):
        children = schema.get(keyword)
        if not isinstance(children, dict):
            continue
        for name, child in children.items():
            token = str(name).replace("~", "~0").replace("/", "~1")
            child_label = (
                name if path == "" and keyword == "properties" else f"{label} / {name}"
            )
            yield from schema_nodes(
                child,
                f"{path}/{keyword}/{token}",
                child_label,
                keyword == "properties" and name in schema.get("required", []),
            )
    for keyword in (
        "items",
        "additionalProperties",
        "contains",
        "propertyNames",
        "not",
        "if",
        "then",
        "else",
        "unevaluatedProperties",
        "unevaluatedItems",
    ):
        child = schema.get(keyword)
        if isinstance(child, dict):
            yield from schema_nodes(child, f"{path}/{keyword}", f"{label} / {keyword}")
        elif keyword == "items" and isinstance(child, list):
            for index, item in enumerate(child):
                yield from schema_nodes(
                    item, f"{path}/{keyword}/{index}", f"{label} / {keyword}[{index}]"
                )
    for keyword in ("anyOf", "oneOf", "allOf", "prefixItems"):
        for index, child in enumerate(schema.get(keyword, [])):
            yield from schema_nodes(
                child, f"{path}/{keyword}/{index}", f"{label} / {keyword}[{index}]"
            )


def identity(tool):
    """Identify the source, including native permission and light-schema proxies."""
    tagged = getattr(tool, "_tool_prompts_identity", None)
    if tagged:
        return tagged
    visited = set()
    while hasattr(tool, "_wrapped") and id(tool) not in visited:
        visited.add(id(tool))
        tool = tool._wrapped
    if isinstance(tool, MCPTool):
        return "mcp", tool.mcp_server_name, tool.name
    module = getattr(tool, "handler_module_path", None)
    if module:
        star = star_map.get(module)
        return "plugin", (star.name if star and star.name else module), tool.name
    if get_builtin_tool_name(type(tool)) is not None:
        return "builtin", "AstrBot Core", tool.name
    module = getattr(getattr(tool, "handler", None), "__module__", None)
    return "other", module or type(tool).__module__, tool.name


def tool_id(tool):
    return json.dumps(identity(tool), ensure_ascii=False, separators=(",", ":"))


def definition_digest(tool):
    data = json.dumps(
        [tool.description, tool.parameters], ensure_ascii=False, sort_keys=True
    )
    return hashlib.sha256(data.encode()).hexdigest()


class DescriptionStore:
    def __init__(self, path: Path):
        self.path = path
        if path.exists():
            data = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(data, dict) or data.get("version") != 1:
                raise ValueError("Unsupported tool description data")
            if type(data.get("revision")) is not int or not isinstance(
                data.get("overrides"), dict
            ):
                raise ValueError("Invalid tool description data")
            for entry in data["overrides"].values():
                self.validate_entry(entry)
            self.revision = data["revision"]
            self.overrides = data["overrides"]
        else:
            self.revision = 0
            self.overrides = {}

    @staticmethod
    def validate_entry(entry):
        if not isinstance(entry, dict) or set(entry) - {"description", "parameters"}:
            raise ValueError("只支持修改工具说明和参数说明")
        if "description" in entry and not isinstance(entry["description"], str):
            raise ValueError("工具说明必须是文本")
        parameters = entry.get("parameters", {})
        if not isinstance(parameters, dict) or any(
            not isinstance(path, str) or not isinstance(text, str)
            for path, text in parameters.items()
        ):
            raise ValueError("参数说明必须是路径与文本的对应表")

    def save(self, key, entry, revision):
        if type(revision) is not int or revision != self.revision:
            raise ConflictError("其他页面已修改配置，请刷新后重新编辑")
        if entry is not None:
            self.validate_entry(entry)
        updated = copy.deepcopy(self.overrides)
        if entry:
            updated[key] = copy.deepcopy(entry)
        else:
            updated.pop(key, None)
        data = {"version": 1, "revision": self.revision + 1, "overrides": updated}
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, name = tempfile.mkstemp(prefix=".tool-prompts-", dir=self.path.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as file:
                json.dump(data, file, ensure_ascii=False, indent=2)
                file.write("\n")
                file.flush()
                os.fsync(file.fileno())
            os.replace(name, self.path)
        finally:
            if os.path.exists(name):
                os.unlink(name)
        self.overrides = updated
        self.revision = data["revision"]

    def effective(self, tool):
        """Make schema-only shallow copies; retain every execution reference."""
        entry = self.overrides.get(tool_id(tool))
        if not entry:
            return tool
        result = copy.copy(tool)
        if getattr(tool, "_tool_prompts_mode", "") != "parameters":
            result.description = entry.get("description", tool.description)
        if (
            entry.get("parameters")
            and getattr(tool, "_tool_prompts_mode", "") != "light"
        ):
            # Schema nodes can share dict instances in Python declarations.
            # JSON round-trip keeps editing one parameter from changing another.
            result.parameters = json.loads(json.dumps(tool.parameters))
            for path, _, node, _ in schema_nodes(result.parameters):
                if path in entry["parameters"]:
                    node["description"] = entry["parameters"][path]
        return result


class ToolCatalog:
    def __init__(self, manager, store):
        self.manager = manager
        self.store = store

    def registered(self):
        tools = {}
        for tool in [*self.manager.func_list, *self.manager.iter_builtin_tools()]:
            key = tool_id(tool)
            previous = tools.get(key)
            if previous is None or tool.active or not previous.active:
                tools[key] = tool
        return tools

    def control(self, tool):
        if self.manager.is_builtin_tool(tool.name):
            return False, "内置工具请在对应功能配置中开启或关闭"
        matches = [item for item in self.manager.func_list if item.name == tool.name]
        if len(matches) != 1 or matches[0] is not tool:
            return False, "存在同名注册，请在 AstrBot 原生工具管理中处理开关"
        star = star_map.get(getattr(tool, "handler_module_path", None))
        if star is not None and not star.activated and not tool.active:
            return False, "所属插件已停用，请先在插件管理中启用插件"
        return True, ""

    async def set_active(self, payload):
        if (
            not isinstance(payload, dict)
            or set(payload) != {"id", "active"}
            or not isinstance(payload["id"], str)
            or type(payload["active"]) is not bool
        ):
            raise ValueError("请提交工具标识和布尔开关值")
        tool = self.registered().get(payload["id"])
        if tool is None:
            raise ConflictError("工具已卸载或断开连接，请刷新列表")
        allowed, reason = self.control(tool)
        if not allowed:
            raise ValueError(reason)
        previous = tool.active
        try:
            if payload["active"]:
                updated = await self.manager.activate_llm_tool_async(
                    tool.name, star_map=star_map
                )
            else:
                updated = await self.manager.deactivate_llm_tool_async(tool.name)
            if not updated:
                raise RuntimeError("Native tool activation update failed")
        except Exception:
            tool.active = previous
            raise
        return self.view()

    def describe(self, tool):
        kind, source, name = identity(tool)
        effective = self.store.effective(tool)
        entry = self.store.overrides.get(tool_id(tool), {})
        parameter_overrides = entry.get("parameters", {})
        originals = {path: node for path, _, node, _ in schema_nodes(tool.parameters)}
        parameters = []
        for path, label, node, required in schema_nodes(effective.parameters):
            if (
                path == ""
                and not node.get("description")
                and path not in parameter_overrides
            ):
                continue
            parameters.append(
                {
                    "path": path,
                    "label": label,
                    "required": required,
                    "type": node.get("type", "$ref" if "$ref" in node else "复合结构"),
                    "enum": node.get("enum"),
                    "default_value": node.get("default"),
                    "has_default": "default" in node,
                    "description": node.get("description", ""),
                    "original_description": originals[path].get("description", ""),
                    "overridden": path in parameter_overrides,
                }
            )
        module = getattr(tool, "handler_module_path", None)
        star = star_map.get(module) if module else None
        can_toggle, toggle_reason = self.control(tool)
        return {
            "id": tool_id(tool),
            "name": name,
            "source_kind": kind,
            "source": source,
            "source_display": (star.display_name or source) if star else source,
            "active": tool.active,
            "can_toggle": can_toggle,
            "toggle_reason": toggle_reason,
            "description": effective.description,
            "original_description": tool.description,
            "overridden": bool(entry),
            "parameters": parameters,
            "parameter_schema": effective.parameters,
            "stale_parameters": [
                path for path in parameter_overrides if path not in originals
            ],
            "definition_digest": definition_digest(tool),
        }

    def view(self):
        tools = [self.describe(tool) for tool in self.registered().values()]
        tools.sort(key=lambda item: (item["source_kind"], item["source"], item["name"]))
        return {"tools": tools, "total": len(tools), "revision": self.store.revision}

    def update(self, payload, *, reset=False):
        fields = {"id", "revision", "definition_digest"}
        if not reset:
            fields |= {"description", "parameters"}
        if not isinstance(payload, dict) or set(payload) != fields:
            raise ValueError("提交内容无效，请刷新页面后重新编辑")
        if not isinstance(payload["id"], str):
            raise ValueError("工具标识无效")
        tool = self.registered().get(payload["id"])
        if tool is None:
            raise ConflictError("工具已卸载或断开连接，请刷新列表")
        if payload["definition_digest"] != definition_digest(tool):
            raise ConflictError("插件已更新工具定义，请刷新后重新编辑")
        entry = {}
        if not reset:
            candidate = {
                "description": payload["description"],
                "parameters": payload["parameters"],
            }
            self.store.validate_entry(candidate)
            nodes = {path: node for path, _, node, _ in schema_nodes(tool.parameters)}
            if set(payload["parameters"]) - nodes.keys():
                raise ValueError("参数结构已改变，请刷新后重新编辑")
            if payload["description"] != tool.description:
                entry["description"] = payload["description"]
            parameters = {
                path: text
                for path, text in payload["parameters"].items()
                if text != nodes[path].get("description", "")
            }
            if parameters:
                entry["parameters"] = parameters
        self.store.save(payload["id"], entry, payload["revision"])
        return self.view()
