"""Edit builtin feature settings through AstrBot's existing profile service."""

from __future__ import annotations

import asyncio
import copy
import hashlib
import json

from astrbot.core.tools.registry import get_builtin_tool_config_rule

from .descriptions import ConflictError

FIELDS = {
    "provider_settings.web_search": ("联网搜索", [False, True], ["关闭", "开启"]),
    "provider_settings.websearch_provider": (
        "搜索服务",
        [
            "tavily",
            "bocha",
            "brave",
            "firecrawl",
            "baidu_ai_search",
            "exa",
            "anysearch",
            "deepseek",
            "serpapi",
        ],
        [
            "Tavily",
            "博查",
            "Brave",
            "Firecrawl",
            "百度 AI 搜索",
            "Exa",
            "AnySearch",
            "DeepSeek",
            "SerpAPI",
        ],
    ),
    "provider_settings.computer_use_runtime": (
        "计算机运行环境",
        ["none", "local", "sandbox"],
        ["关闭", "本机", "沙箱"],
    ),
    "provider_settings.sandbox.booter": (
        "沙箱类型",
        ["shipyard_neo", "shipyard", "cua"],
        ["Shipyard Neo", "Shipyard", "CUA"],
    ),
    "provider_settings.proactive_capability.add_cron_tools": (
        "定时任务工具",
        [False, True],
        ["关闭", "开启"],
    ),
    "kb_agentic_mode": ("知识库工具模式", [False, True], ["关闭", "开启"]),
    "provider_ltm_settings.group_message_history_enable": (
        "群聊历史记录",
        [False, True],
        ["关闭", "开启"],
    ),
}


def value_at(config, path):
    value = config
    for part in path.split("."):
        if not isinstance(value, dict) or part not in value:
            return None
        value = value[part]
    return value


def set_value(config, path, value):
    parts = path.split(".")
    for part in parts[:-1]:
        config = config.setdefault(part, {})
    config[parts[-1]] = value


def revision(config):
    # Only editable values: unrelated native-page changes are merged at save time.
    values = {path: value_at(config, path) for path in FIELDS}
    return hashlib.sha256(json.dumps(values, sort_keys=True).encode()).hexdigest()


class NativeConfigs:
    def __init__(self, manager):
        self.manager = manager
        self.lock = asyncio.Lock()

    def config(self, profile_id):
        if not isinstance(profile_id, str) or profile_id not in self.manager.confs:
            raise ConflictError("配置档案已删除，请刷新页面")
        return self.manager.confs[profile_id]

    def view(self, catalog, profile_id):
        config = self.config(profile_id)
        catalog["profiles"] = [
            {"id": item["id"], "name": item["name"]}
            for item in self.manager.get_conf_list()
        ]
        catalog["profile_id"] = profile_id
        catalog["config_revision"] = revision(config)
        builtins = [
            tool for tool in catalog["tools"] if tool["source_kind"] == "builtin"
        ]
        rules = {
            tool["name"]: get_builtin_tool_config_rule(tool["name"])
            for tool in builtins
        }
        for tool in builtins:
            rule = rules[tool["name"]]
            conditions = rule.evaluate(config) if rule else []
            tool["active"] = bool(conditions) and all(c["matched"] for c in conditions)
            keys = [c.key for c in rule.conditions if c.key in FIELDS] if rule else []
            fields = []
            for key in keys:
                label, values, labels = FIELDS[key]
                fields.append(
                    {
                        "key": key,
                        "label": label,
                        "value": value_at(config, key),
                        "options": [
                            {"value": v, "label": text}
                            for v, text in zip(values, labels)
                        ],
                    }
                )
            requirements = []
            for c in rule.conditions if rule else []:
                if c.key not in FIELDS:
                    continue
                label, values, labels = FIELDS[c.key]
                expected = c.expected if c.operator == "in" else [c.expected]
                text = " / ".join(
                    labels[values.index(v)] for v in expected if v in values
                )
                requirements.append(f"{label}：{text}")
            linked = sorted(
                name
                for name, other in rules.items()
                if name != tool["name"]
                and other
                and any(c.key in keys for c in other.conditions)
            )
            tool["can_toggle"] = False
            tool["toggle_reason"] = ""
            tool["native"] = {
                "fields": fields,
                "requirements": requirements,
                "linked_tools": linked,
                "note": (
                    "直接保存当前档案的原功能配置，关联工具同步变化。服务凭证、运行环境和权限沿用原设置。"
                    if fields
                    else "此工具由平台能力自动提供，没有独立的原功能开关；平台配置继续在原配置页管理。"
                ),
            }
        return catalog

    async def update(self, payload, profile_id, service):
        if not isinstance(payload, dict) or set(payload) != {"revision", "values"}:
            raise ValueError("原配置提交内容无效")
        values = payload["values"]
        if not isinstance(values, dict) or not values or set(values) - FIELDS.keys():
            raise ValueError("只能修改页面提供的工具功能配置")
        for key, value in values.items():
            if not any(type(value) is type(v) and value == v for v in FIELDS[key][1]):
                raise ValueError(f"{FIELDS[key][0]}的值无效")
        async with self.lock:
            current = self.config(profile_id)
            if payload["revision"] != revision(current):
                raise ConflictError("原功能配置已在其他页面修改，请刷新后重新编辑")
            updated = copy.deepcopy(dict(current))
            for key, value in values.items():
                set_value(updated, key, value)
            # Same validation, native persistence and pipeline reload as WebUI.
            return await service.update_profile(
                profile_id, updated, allow_admin_id_change=False
            )
