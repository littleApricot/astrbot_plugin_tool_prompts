"""Own prompt placement and the boundary between instructions and source data."""

from __future__ import annotations

import copy

from .tool_instructions import tool_view

# Producers declare kinds and content; placement decisions belong here.
PLACEMENTS = {
    "memory": "system",
    "notebook_catalog": "system",
    "tool_guidance": "system",
    "tool_definitions": "system",
    "runtime": "temporary",
    "meme_candidates": "temporary",
    "memory_reminder": "temporary",
    "legacy_system": "system",
    "legacy_temporary": "temporary",
}


def source_entries(messages):
    """Keep actual messages and executions; exclude typed injected material."""
    result = []
    for index, source in enumerate(messages):
        value = source if isinstance(source, dict) else source.model_dump(mode="json")
        if value.get("role") not in {"user", "assistant", "tool"}:
            continue
        if value.get("_no_save") or value.get("_context_kind"):
            continue
        value = copy.deepcopy(value)
        for key in ("tools", "functions", "func_tool", "_context_kind"):
            value.pop(key, None)
        content = value.get("content")
        if isinstance(content, list):
            kept = [
                part
                for part in content
                if not isinstance(part, dict)
                or not (part.get("_no_save") or part.get("_context_kind"))
            ]
            if content and not kept and not value.get("tool_calls"):
                continue
            value["content"] = kept
        result.append((index, value))
    return result


class PromptPolicy:
    def __init__(self, context):
        self.context = context

    @staticmethod
    def blocks(event, declarations, *, position=None, kinds=None):
        result = []
        for declaration in declarations:
            blocks = declaration.get("prompt_blocks")
            if blocks is None:
                blocks = [
                    {
                        "key": field,
                        "kind": "legacy_" + field,
                        "content": declaration[field],
                    }
                    for field in ("system", "temporary")
                    if field in declaration
                ]
            if not isinstance(blocks, list):
                raise ValueError("提示内容声明必须是列表")
            seen = set()
            for block in blocks:
                if not isinstance(block, dict):
                    raise ValueError("提示内容声明必须是对象")
                key, kind = block.get("key"), block.get("kind")
                if not isinstance(key, str) or not key or key in seen:
                    raise ValueError("提示内容必须有唯一标识")
                if not isinstance(kind, str) or not kind:
                    raise ValueError("提示内容必须声明类型")
                seen.add(key)
                if (
                    position is not None
                    and PLACEMENTS.get(kind, "temporary") != position
                ):
                    continue
                if kinds is not None and kind not in kinds:
                    continue
                content = block.get("content", "")
                text = content(event) if callable(content) else content
                if not isinstance(text, str):
                    raise ValueError("提示内容必须是文本")
                if text.strip():
                    result.append(
                        {
                            "owner": declaration["owner"],
                            "key": key,
                            "kind": kind,
                            "text": text,
                        }
                    )
        return result

    def records(self, event, declarations, position, additional=()):
        blocks = [*additional, *self.blocks(event, declarations, position=position)]
        grouped = {}
        for block in sorted(blocks, key=lambda block: (block["owner"], block["key"])):
            if PLACEMENTS.get(block["kind"], "temporary") == position:
                grouped.setdefault(block["owner"], []).append(block["text"])
        return [(owner, "\n\n".join(texts)) for owner, texts in grouped.items()]

    def tools(self, event, available, catalog):
        instructions, calls = tool_view(available, catalog)
        blocks = (
            [
                {
                    "owner": "astrbot.tools",
                    "key": "definitions",
                    "kind": "tool_definitions",
                    "text": instructions,
                }
            ]
            if instructions
            else []
        )
        return calls, blocks

    def declarations(self):
        lookup = getattr(self.context, "get_registered_star", None)
        star = lookup("astrbot_plugin_lcm") if callable(lookup) else None
        plugin = getattr(star, "star_cls", None)
        if (
            plugin
            and getattr(star, "activated", True)
            and getattr(plugin, "config", {}).get("enabled", True)
        ):
            return plugin.context_rules().declarations()
        get_all = getattr(self.context, "get_all_stars", None)
        result = []
        for star in get_all() if callable(get_all) else []:
            plugin = getattr(star, "star_cls", None)
            declare = getattr(plugin, "lcm_context_rules", None)
            if (
                callable(declare)
                and getattr(star, "activated", True)
                and getattr(plugin, "config", {}).get("enabled", True)
            ):
                result.append(declare())
        return result

    def decision_memory(self, event, fallback=""):
        # Only the explicitly declared memory is eligible. A rendered system
        # prompt can also contain tools/catalogs and must never become memory.
        declarations = self.declarations()
        declared = any(
            block.get("kind") == "memory"
            for declaration in declarations
            for block in declaration.get("prompt_blocks", [])
        )
        memory = [
            block["text"]
            for block in self.blocks(event, declarations, kinds={"memory"})
        ]
        return "\n\n".join(memory) if declared else fallback

    @staticmethod
    def history_entries(messages):
        return source_entries(messages)
