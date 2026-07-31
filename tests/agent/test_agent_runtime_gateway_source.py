from types import SimpleNamespace

from agent import agent_runtime_helpers
from gateway.config import Platform
from gateway.run import GatewayRunner
from gateway.session import SessionContext, SessionSource


def test_gateway_tool_source_uses_exact_turn_local_owner_context():
    """BUILD-695: tool dispatch must not reconstruct owner identity from env."""
    source = SessionSource(
        platform=Platform.TELEGRAM,
        chat_id="marketing-chat",
        chat_type="group",
        thread_id="forum-topic-695",
        user_id="nicholas",
        scope_id="marketing-workspace",
        profile="marketing",
    )
    context = SessionContext(
        source=source,
        connected_platforms=[Platform.TELEGRAM],
        home_channels={},
        session_key="agent:marketing:telegram:group:marketing-chat:forum-topic-695",
        session_id="marketing-session-695",
    )
    runner = GatewayRunner.__new__(GatewayRunner)
    runner.adapters = {}
    tokens = runner._set_session_env(context)
    try:
        build_source = getattr(
            agent_runtime_helpers,
            "_gateway_source_for_agent",
            lambda _agent: None,
        )
        owner = build_source(SimpleNamespace(
            platform="telegram",
            chat_id="marketing-chat",
            chat_type="group",
            thread_id="forum-topic-695",
            user_id="nicholas",
            session_id="marketing-session-695",
            _gateway_session_key=context.session_key,
        ))
    finally:
        runner._clear_session_env(tokens)

    assert owner == {
        "profile": "marketing",
        "session_id": "marketing-session-695",
        "session_key": context.session_key,
        "platform": "telegram",
        "chat_id": "marketing-chat",
        "chat_type": "group",
        "thread_id": "forum-topic-695",
        "user_id": "nicholas",
        "scope_id": "marketing-workspace",
    }
