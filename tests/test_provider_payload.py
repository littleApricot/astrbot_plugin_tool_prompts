import json

import httpx
import pytest
from astrbot.core.agent.tool import ToolSet
from astrbot.core.provider.sources.openai_source import ProviderOpenAIOfficial
from openai import AsyncOpenAI
from test_descriptions import save
from test_descriptions import setup as setup_fixture

setup = setup_fixture


@pytest.mark.anyio
async def test_saved_descriptions_reach_native_openai_http_payload(setup):
    save(setup)
    calls = []

    def respond(request):
        calls.append(json.loads(request.content))
        return httpx.Response(
            200,
            json={
                "id": "isolated",
                "object": "chat.completion",
                "created": 0,
                "model": "isolated",
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": "offline reply"},
                        "finish_reason": "stop",
                    }
                ],
            },
        )

    provider = ProviderOpenAIOfficial(
        {
            "id": "isolated",
            "type": "openai_chat_completion",
            "key": ["isolated-test-marker"],
            "model": "isolated",
            "api_base": "https://isolated.invalid/v1",
        },
        {},
    )
    await provider.client.close()
    provider.client = AsyncOpenAI(
        api_key="isolated-test-marker",
        base_url="https://isolated.invalid/v1",
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(respond)),
    )
    try:
        await provider.text_chat(
            prompt="offline", func_tool=setup.manager.get_full_tool_set()
        )
        definition = calls[0]["tools"][0]["function"]
        assert definition["description"] == "新工具说明"
        assert (
            definition["parameters"]["properties"]["query"]["description"]
            == "新查询说明"
        )
        assert calls[0]["messages"][-1]["content"] == "offline"
        save(setup, description="", parameters={"/properties/query": ""})
        await provider.text_chat(prompt="cleared", func_tool=ToolSet([setup.tool]))
        cleared = calls[1]["tools"][0]["function"]
        assert "description" not in cleared
        assert "description" not in cleared["parameters"]["properties"]["query"]
        assert cleared["parameters"]["required"] == ["query"]
        setup.bridge.stop()
        await provider.text_chat(
            prompt="offline again", func_tool=ToolSet([setup.tool])
        )
        assert calls[2]["tools"][0]["function"]["description"] == "原始工具说明"
    finally:
        await provider.client.close()
