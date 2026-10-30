import os

import pytest

from model_gateway import ChatMessage, ChatRequest, ModelGateway


pytestmark = pytest.mark.skipif(
    not os.environ.get("DEEPSEEK_API_KEY") and not os.environ.get("ANTHROPIC_API_KEY"),
    reason="设置 DEEPSEEK_API_KEY 或 ANTHROPIC_API_KEY 后再跑真实调用",
)


@pytest.mark.skipif(not os.environ.get("DEEPSEEK_API_KEY"), reason="需要 DEEPSEEK_API_KEY")
def test_live_deepseek(tmp_path):
    gateway = ModelGateway.from_env(db_path=tmp_path / "live.db")
    response = gateway.complete(
        ChatRequest(
            provider="deepseek",
            model="deepseek-v4-flash",
            messages=[ChatMessage(role="user", content="Reply with the single word ok")],
            max_tokens=16,
        )
    )
    assert response.content
    assert response.usage.total_tokens > 0
    assert response.cost.total_cost >= 0
    assert gateway.usage.summary(provider="deepseek").attempts == 1
    gateway.close()


@pytest.mark.skipif(not os.environ.get("ANTHROPIC_API_KEY"), reason="需要 ANTHROPIC_API_KEY")
def test_live_anthropic(tmp_path):
    gateway = ModelGateway.from_env(db_path=tmp_path / "live.db")
    response = gateway.complete(
        ChatRequest(
            provider="anthropic",
            model="claude-sonnet-5-5",
            messages=[ChatMessage(role="user", content="Reply with the single word ok")],
            max_tokens=16,
        )
    )
    assert response.content
    assert response.usage.total_tokens > 0
    gateway.close()
