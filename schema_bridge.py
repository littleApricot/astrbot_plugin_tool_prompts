"""Apply descriptions at native schema serialization, without mutating tools."""

from __future__ import annotations

import copy
from functools import wraps

from astrbot.core.agent.tool import ToolSet

from .descriptions import identity

_STATE = "_littleapricot_tool_prompts_bridge"


class SchemaBridge:
    def __init__(self, store):
        self.store = store
        self.active = False
        self.originals = {}
        self.wrappers = {}

    def start(self):
        previous = getattr(ToolSet, _STATE, None)
        if previous is not None:
            previous.stop()
        self.active = True
        for method in ("openai_schema", "anthropic_schema", "google_schema"):
            original = getattr(ToolSet, method)
            self.originals[method] = original
            wrapper = self._serializer(original)
            self.wrappers[method] = wrapper
            setattr(ToolSet, method, wrapper)
        for method, mode in (
            ("get_light_tool_set", "light"),
            ("get_param_only_tool_set", "parameters"),
        ):
            original = getattr(ToolSet, method)
            self.originals[method] = original
            wrapper = self._shape(original, mode)
            self.wrappers[method] = wrapper
            setattr(ToolSet, method, wrapper)
        setattr(ToolSet, _STATE, self)

    def _serializer(self, original):
        @wraps(original)
        def serialize(toolset, *args, **kwargs):
            if not self.active:
                return original(toolset, *args, **kwargs)
            proxy = copy.copy(toolset)
            proxy.tools = [self.store.effective(tool) for tool in toolset.tools]
            return original(proxy, *args, **kwargs)

        return serialize

    def _shape(self, original, mode):
        @wraps(original)
        def shape(toolset, *args, **kwargs):
            result = original(toolset, *args, **kwargs)
            if self.active:
                sources = {tool.name: tool for tool in toolset.tools}
                for tool in result.tools:
                    tool._tool_prompts_identity = identity(sources[tool.name])
                    tool._tool_prompts_mode = mode
            return result

        return shape

    def stop(self):
        self.active = False
        for name, original in self.originals.items():
            if getattr(ToolSet, name) is self.wrappers[name]:
                setattr(ToolSet, name, original)
        if getattr(ToolSet, _STATE, None) is self:
            delattr(ToolSet, _STATE)
