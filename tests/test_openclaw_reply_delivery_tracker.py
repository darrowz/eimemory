from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]


def _isolated_env() -> dict[str, str]:
    env = os.environ.copy()
    for name in (
        "EIMEMORY_RPC_URL",
        "EIMEMORY_RPC_AUTH_TOKEN",
        "EIMEMORY_RPC_TOKEN",
    ):
        env.pop(name, None)
    return env


def _run_node(
    script: str,
    state_path: Path,
) -> dict:
    env = _isolated_env()
    env["EIMEMORY_REPLY_DELIVERY_STATE_PATH"] = str(state_path)
    env["EIMEMORY_RELEASE_CLOSURE_SIGNAL_PATH"] = str(
        state_path.with_name("release-closure-channel-receipt.signal")
    )
    env["EIMEMORY_HOOK_COMMAND"] = "/usr/bin/true"
    env["EIMEMORY_RUNTIME_COMMIT"] = "a" * 40
    env["OPENCLAW_CONFIG_PATH"] = str(state_path.with_name("openclaw.json"))
    result = subprocess.run(
        ["node", "-e", script],
        cwd=ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        env=env,
        timeout=10,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    return json.loads(state_path.read_text(encoding="utf-8"))


def test_tracker_ignores_inbound_without_feishu_reply_correlation(
    tmp_path: Path,
) -> None:
    state = _run_node(
        """
const plugin = require('./integrations/openclaw/eimemory-bridge/index.js').default;
const handlers = {};
plugin.register({ on(name, handler) { handlers[name] = handler; } });
Promise.resolve(handlers.message_received({
  content: 'internal event', messageId: 'internal-event'
}, {
  channelId: 'feishu',
  sessionKey: 'agent:main:feishu:direct:unknown'
}));
""",
        tmp_path / "reply-state.json",
    )

    assert state["entries"] == {}


def test_tracker_correlates_inbound_final_and_platform_receipt(tmp_path: Path) -> None:
    state = _run_node(
        """
const plugin = require('./integrations/openclaw/eimemory-bridge/index.js').default;
const handlers = {};
plugin.register({ on(name, handler) { handlers[name] = handler; } });
const ctx = {
  channelId: 'feishu',
  conversationId: 'oc_test',
  sessionKey: 'agent:main:feishu:direct:ou_test'
};
Promise.resolve()
  .then(() => handlers.message_received({
    from: 'ou_test',
    content: '测试首条回复',
    messageId: 'om_in_100',
    sessionKey: ctx.sessionKey
  }, ctx))
  .then(() => handlers.agent_end({
    success: true,
    messages: [
      { role: 'user', content: '测试首条回复' },
      { role: 'assistant', content: [{ type: 'text', text: '这是最终答复' }] }
    ]
  }, ctx))
  .then(() => handlers.message_sent({
    to: 'ou_test',
    content: '这是最终答复',
    success: true,
    messageId: 'om_out_100',
    sessionKey: ctx.sessionKey
  }, ctx));
""",
        tmp_path / "reply-state.json",
    )

    entry = state["entries"]["om_in_100"]
    assert entry["status"] == "platform_accepted"
    assert entry["final_text"] == "这是最终答复"
    assert entry["delivery_message_id"] == "om_out_100"
    assert entry["conversation_id"] == "oc_test"
    assert entry["runtime_commit"] == "a" * 40
    signal = json.loads(
        (tmp_path / "release-closure-channel-receipt.signal").read_text(encoding="utf-8")
    )
    assert signal == {
        "schema_version": "release_closure_channel_receipt_signal.v1",
        "runtime_commit": "a" * 40,
        "platform_accepted_at_ms": entry["platform_accepted_at_ms"],
    }


def test_tracker_correlates_official_sessionless_message_sent_by_destination(
    tmp_path: Path,
) -> None:
    state = _run_node(
        """
const plugin = require('./integrations/openclaw/eimemory-bridge/index.js').default;
const handlers = {};
plugin.register({ on(name, handler) { handlers[name] = handler; } });
const sessionKey = 'agent:main:feishu:direct:ou_test';
Promise.resolve()
  .then(() => handlers.message_received({
    from: 'ou_test',
    content: 'verify deployment',
    messageId: 'om_sessionless_in',
    sessionKey
  }, {
    channelId: 'feishu',
    conversationId: 'oc_sessionless',
    sessionKey
  }))
  .then(() => handlers.agent_end({
    success: true,
    messages: [{ role: 'assistant', content: 'deployment verified' }]
  }, { sessionKey }))
  .then(() => handlers.message_sent({
    to: 'oc_sessionless',
    content: 'deployment verified',
    success: true,
    messageId: 'om_sessionless_out'
  }, {
    channelId: 'feishu',
    conversationId: 'oc_sessionless'
  }));
""",
        tmp_path / "reply-state.json",
    )

    entry = state["entries"]["om_sessionless_in"]
    assert entry["status"] == "platform_accepted"
    assert entry["delivery_message_id"] == "om_sessionless_out"
    assert entry["runtime_commit"] == "a" * 40


def test_tracker_accepts_real_agent_hook_feishu_context(tmp_path: Path) -> None:
    state = _run_node(
        """
const plugin = require('./integrations/openclaw/eimemory-bridge/index.js').default;
const handlers = {};
plugin.register({ on(name, handler) { handlers[name] = handler; } });
const sessionKey = 'agent:main:feishu:direct:ou_test';
Promise.resolve()
  .then(() => handlers.message_received({
    from: 'ou_test', messageId: 'om_real_context', runId: 'run-real'
  }, {
    channelId: 'feishu',
    conversationId: 'user:ou_test',
    sessionKey,
    runId: 'run-real'
  }))
  .then(() => handlers.agent_end({
    success: true,
    runId: 'run-real',
    messages: [{ role: 'assistant', content: '生产上下文最终答复' }]
  }, {
    messageProvider: 'feishu',
    channel: 'ou_test',
    channelId: 'ou_test',
    sessionKey,
    runId: 'run-real'
  }));
""",
        tmp_path / "reply-state.json",
    )

    entry = state["entries"]["om_real_context"]
    assert entry["status"] == "final_ready"
    assert entry["final_text"] == "生产上下文最终答复"


def test_tracker_accepts_agent_end_with_session_only_context(tmp_path: Path) -> None:
    state = _run_node(
        """
const plugin = require('./integrations/openclaw/eimemory-bridge/index.js').default;
const handlers = {};
plugin.register({ on(name, handler) { handlers[name] = handler; } });
const sessionKey = 'agent:main:feishu:direct:ou_test';
Promise.resolve()
  .then(() => handlers.message_received({
    from: 'ou_test', messageId: 'om_session_only', runId: 'run-session-only'
  }, {
    channelId: 'feishu', conversationId: 'user:ou_test', sessionKey,
    runId: 'run-session-only'
  }))
  .then(() => handlers.agent_end({
    success: true,
    runId: 'run-session-only',
    messages: [{ role: 'assistant', content: 'session-only final' }]
  }, {
    sessionKey,
    runId: 'run-session-only'
  }));
""",
        tmp_path / "reply-state.json",
    )

    entry = state["entries"]["om_session_only"]
    assert entry["status"] == "final_ready"
    assert entry["final_text"] == "session-only final"


def test_tracker_closes_message_tool_receipt_without_message_sent_hook(
    tmp_path: Path,
) -> None:
    state = _run_node(
        """
const plugin = require('./integrations/openclaw/eimemory-bridge/index.js').default;
const handlers = {};
plugin.register({ on(name, handler) { handlers[name] = handler; } });
const sessionKey = 'agent:main:feishu:direct:ou_test';
const receipt = {
  ok: true,
  channel: 'feishu',
  action: 'send',
  messageId: 'om_tool_receipt',
  receipt: { primaryPlatformMessageId: 'om_tool_receipt' }
};
Promise.resolve()
  .then(() => handlers.message_received({
    from: 'ou_test', messageId: 'om_tool_inbound', runId: 'run-tool'
  }, {
    channelId: 'feishu', conversationId: 'user:ou_test', sessionKey,
    runId: 'run-tool'
  }))
  .then(() => handlers.after_tool_call({
    toolName: 'message',
    params: { action: 'send', message: 'tool-delivered reply' },
    runId: 'run-tool',
    result: { content: [{ type: 'text', text: JSON.stringify(receipt) }] }
  }, {
    sessionKey,
    runId: 'run-tool',
    toolName: 'message'
  }));
""",
        tmp_path / "reply-state.json",
    )

    entry = state["entries"]["om_tool_inbound"]
    assert entry["status"] == "platform_accepted"
    assert entry["final_text"] == "tool-delivered reply"
    assert entry["delivery_message_id"] == "om_tool_receipt"


def test_tracker_rejects_invalid_message_tool_platform_receipt(tmp_path: Path) -> None:
    state = _run_node(
        """
const plugin = require('./integrations/openclaw/eimemory-bridge/index.js').default;
const handlers = {};
plugin.register({ on(name, handler) { handlers[name] = handler; } });
const sessionKey = 'agent:main:feishu:direct:ou_test';
Promise.resolve()
  .then(() => handlers.message_received({
    from: 'ou_test', messageId: 'om_invalid_receipt_inbound', runId: 'run-invalid'
  }, {
    channelId: 'feishu', conversationId: 'user:ou_test', sessionKey,
    runId: 'run-invalid'
  }))
  .then(() => handlers.after_tool_call({
    toolName: 'message',
    params: { action: 'send', message: 'must remain pending' },
    runId: 'run-invalid',
    result: {
      content: [{
        type: 'text',
        text: JSON.stringify({
          ok: true,
          channel: 'feishu',
          receipt: { primaryPlatformMessageId: 'not-a-message-id' }
        })
      }]
    }
  }, {
    sessionKey,
    runId: 'run-invalid',
    toolName: 'message'
  }));
""",
        tmp_path / "reply-state.json",
    )

    entry = state["entries"]["om_invalid_receipt_inbound"]
    assert entry["status"] == "pending"
    assert entry["delivery_message_id"] == ""


def test_tracker_rejects_nested_success_inside_failed_message_tool_result(
    tmp_path: Path,
) -> None:
    state = _run_node(
        """
const plugin = require('./integrations/openclaw/eimemory-bridge/index.js').default;
const handlers = {};
plugin.register({ on(name, handler) { handlers[name] = handler; } });
const sessionKey = 'agent:main:feishu:direct:ou_test';
Promise.resolve()
  .then(() => handlers.message_received({
    from: 'ou_test', messageId: 'om_failed_wrapper', runId: 'run-failed-wrapper'
  }, {
    channelId: 'feishu', conversationId: 'user:ou_test', sessionKey,
    runId: 'run-failed-wrapper'
  }))
  .then(() => handlers.after_tool_call({
    toolName: 'message',
    params: { action: 'send', message: 'must remain pending' },
    runId: 'run-failed-wrapper',
    result: {
      ok: false,
      data: {
        ok: true,
        channel: 'feishu',
        messageId: 'om_nested_success'
      }
    }
  }, {
    sessionKey,
    runId: 'run-failed-wrapper',
    toolName: 'message'
  }));
""",
        tmp_path / "reply-state.json",
    )

    entry = state["entries"]["om_failed_wrapper"]
    assert entry["status"] == "pending"
    assert entry["delivery_message_id"] == ""


def test_tracker_records_progress_for_active_tool_calls(tmp_path: Path) -> None:
    state = _run_node(
        """
const plugin = require('./integrations/openclaw/eimemory-bridge/index.js').default;
const handlers = {};
plugin.register({ on(name, handler) { handlers[name] = handler; } });
const sessionKey = 'agent:main:feishu:direct:ou_test';
const ctx = {
  channelId: 'feishu', conversationId: 'user:ou_test', sessionKey,
  runId: 'run-progress'
};
Promise.resolve()
  .then(() => handlers.message_received({
    from: 'ou_test', messageId: 'om_progress', runId: 'run-progress', timestamp: 1
  }, ctx))
  .then(() => handlers.before_tool_call({
    toolName: 'bash', runId: 'run-progress'
  }, ctx));
""",
        tmp_path / "reply-state.json",
    )

    entry = state["entries"]["om_progress"]
    assert entry["status"] == "pending"
    assert entry["last_progress_at_ms"] > entry["received_at_ms"]


def test_tracker_ignores_group_messages(tmp_path: Path) -> None:
    state_path = tmp_path / "reply-state.json"
    state = _run_node(
        """
const plugin = require('./integrations/openclaw/eimemory-bridge/index.js').default;
const handlers = {};
plugin.register({ on(name, handler) { handlers[name] = handler; } });
Promise.resolve(handlers.message_received({
  from: 'ou_test', content: '群消息', messageId: 'om_group0'
}, {
  channelId: 'feishu',
  conversationId: 'oc_group',
  sessionKey: 'agent:main:feishu:group:oc_group'
}));
""",
        state_path,
    )

    assert state["entries"] == {}


def test_tracker_accepts_tool_delivery_that_precedes_agent_end(tmp_path: Path) -> None:
    state = _run_node(
        """
const plugin = require('./integrations/openclaw/eimemory-bridge/index.js').default;
const handlers = {};
plugin.register({ on(name, handler) { handlers[name] = handler; } });
const ctx = {
  channelId: 'feishu', conversationId: 'oc_test',
  sessionKey: 'agent:main:feishu:direct:ou_test'
};
Promise.resolve()
  .then(() => handlers.message_received({
    from: 'ou_test', content: '测试', messageId: 'om_in_200'
  }, ctx))
  .then(() => handlers.message_sent({
    to: 'ou_test', content: '工具直接答复', success: true, messageId: 'om_out_200'
  }, ctx))
  .then(() => handlers.agent_end({
    success: true,
    messages: [{ role: 'assistant', content: '工具直接答复' }]
  }, ctx));
""",
        tmp_path / "reply-state.json",
    )

    entry = state["entries"]["om_in_200"]
    assert entry["status"] == "platform_accepted"
    assert entry["delivery_message_id"] == "om_out_200"


def test_tracker_ignores_pre_compaction_no_reply_turn(tmp_path: Path) -> None:
    state = _run_node(
        """
const plugin = require('./integrations/openclaw/eimemory-bridge/index.js').default;
const handlers = {};
plugin.register({ on(name, handler) { handlers[name] = handler; } });
const ctx = {
  channelId: 'feishu', conversationId: 'oc_test',
  sessionKey: 'agent:main:feishu:direct:ou_test'
};
Promise.resolve()
  .then(() => handlers.message_received({
    from: 'ou_test', content: '正常问题', messageId: 'om_in_300'
  }, ctx))
  .then(() => handlers.agent_end({
    success: true,
    messages: [
      { role: 'user', content: 'Pre-compaction memory flush. Store durable memories only.' },
      { role: 'assistant', content: 'NO_REPLY' }
    ]
  }, ctx));
""",
        tmp_path / "reply-state.json",
    )

    entry = state["entries"]["om_in_300"]
    assert entry["status"] == "silent"
    assert entry["final_text"] == ""
    assert entry["suppress_stalled_notice"] is True


def test_tracker_closes_gateway_final_receipt_even_if_agent_end_came_first(tmp_path: Path) -> None:
    """The compatibility hook may still close an automatic Feishu final."""
    state = _run_node(
        """
const plugin = require('./integrations/openclaw/eimemory-bridge/index.js').default;
const handlers = {};
plugin.register({ on(name, handler) { handlers[name] = handler; } });
const ctx = {
  channelId: 'feishu',
  conversationId: 'user:ou_test',
  chatId: 'oc_real_chat',
  sessionKey: 'agent:main:feishu:direct:ou_test'
};
Promise.resolve()
  .then(() => handlers.message_received({
    from: 'ou_test', messageId: 'om_auto_final', runId: 'run-auto'
  }, ctx))
  .then(() => handlers.agent_end({
    success: true,
    runId: 'run-auto',
    messages: [{ role: 'assistant', content: '同一条最终答复' }]
  }, ctx))
  .then(() => handlers.message_sent({
    content: '同一条最终答复\\n',
    success: true,
    messageId: 'om_gateway_out',
    chatId: 'oc_real_chat'
  }, ctx));
""",
        tmp_path / "reply-state.json",
    )

    entry = state["entries"]["om_auto_final"]
    assert entry["status"] == "platform_accepted"
    assert entry["delivery_message_id"] == "om_gateway_out"
    assert entry["conversation_id"] == "oc_real_chat"


def test_tracker_requires_nonempty_platform_receipt(tmp_path: Path) -> None:
    state = _run_node(
        """
const plugin = require('./integrations/openclaw/eimemory-bridge/index.js').default;
const handlers = {};
plugin.register({ on(name, handler) { handlers[name] = handler; } });
const ctx = { channelId: 'feishu', conversationId: 'oc_test', sessionKey: 'agent:main:feishu:direct:ou_test' };
Promise.resolve()
  .then(() => handlers.message_received({ from: 'ou_test', messageId: 'om_in_blank' }, ctx))
  .then(() => handlers.agent_end({ success: true, messages: [{ role: 'assistant', content: '答复' }] }, ctx))
  .then(() => handlers.message_sent({ content: '答复', success: true, messageId: '' }, ctx));
""",
        tmp_path / "reply-state.json",
    )

    assert state["entries"]["om_in_blank"]["status"] == "final_ready"
    assert state["entries"]["om_in_blank"]["delivery_message_id"] == ""


def _run_external_delivery_probe(tmp_path: Path, *, result=None, failure=False, config=None, sdk_missing=False):
    state_path = tmp_path / "reply-state.json"
    capture_path = tmp_path / "gateway-calls.json"
    config = config if config is not None else {"mode": "local", "port": 18789, "auth": {"mode": "token"}}
    script = r"""
const fs = require('node:fs');
const Module = require('node:module');
const originalLoad = Module._load;
const calls = [], warnings = [];
let privilegedCalls = 0;
const options = OPTIONS;
Module._load = function(specifier, ...args) {
  if (specifier === 'openclaw/plugin-sdk/gateway-runtime') {
    if (options.sdkMissing) throw new Error('secret-token-do-not-log');
    return { async callGatewayFromCli(...request) {
      calls.push(request);
      if (options.failure && calls.length === 1) throw new Error('secret-token-do-not-log');
      return options.result;
    }};
  }
  return originalLoad.call(this, specifier, ...args);
};
const handlers = {};
require('./integrations/openclaw/eimemory-bridge/index.js').default.register({
  config: {gateway: options.config},
  runtime: {gateway: {request() { privilegedCalls += 1; throw new Error('privileged runtime must not run'); }}},
  logger: {warn(value) { warnings.push(value); }},
  on(name, handler) { handlers[name] = handler; },
});
const ctx = {channelId:'feishu', conversationId:'oc_test', sessionKey:'agent:main:feishu:direct:ou_test'};
const final = {success:true, runId:'run-external', messages:[{role:'assistant', content:'Authorized final reply'}]};
async function settle() { await new Promise(resolve => setTimeout(resolve, 30)); }
(async () => {
  await handlers.message_received({from:'ou_test', messageId:'om_external_in', runId:'run-external'}, ctx);
  await handlers.agent_end(final, ctx);
  await settle();
  await handlers.agent_end(final, ctx);
  await settle();
  fs.writeFileSync(CAPTURE, JSON.stringify({calls,warnings,privilegedCalls}));
})().catch(error => { console.error(error); process.exitCode = 1; });
""".replace("OPTIONS", json.dumps({"config": config, "result": result, "failure": failure, "sdkMissing": sdk_missing}))
    script = script.replace("CAPTURE", json.dumps(str(capture_path)))
    state = _run_node(script, state_path)
    return state, json.loads(capture_path.read_text(encoding="utf-8"))


def test_external_plugin_delivers_through_authenticated_public_sdk(tmp_path: Path) -> None:
    state, captured = _run_external_delivery_probe(tmp_path, result={
        "ok": True, "channel": "feishu", "messageId": "om_external_out",
    })
    assert captured["privilegedCalls"] == 0
    assert len(captured["calls"]) == 1
    method, options, request, extra = captured["calls"][0]
    assert method == "message.action"
    assert options == {"port": "18789", "timeout": "20000", "expectFinal": True, "json": True}
    assert extra == {"clientName": "gateway-client", "mode": "backend", "progress": False,
                     "scopes": ["operator.write"], "sharedStateMode": "read-only"}
    assert request["action"] == "send" and request["channel"] == "feishu"
    assert request["params"] == {"to": "ou_test", "message": "Authorized final reply"}
    assert request["sessionKey"] == "agent:main:feishu:direct:ou_test"
    assert request["idempotencyKey"].startswith("eimemory-probe:")
    assert state["entries"]["om_external_in"]["delivery_message_id"] == "om_external_out"
    assert state["entries"]["om_external_in"]["status"] == "platform_accepted"


@pytest.mark.parametrize("result", [
    {"ok": True, "channel": "feishu", "messageId": ""},
    {"ok": True, "channel": "feishu", "messageId": "synthetic-success"},
    {"ok": False, "payload": {"ok": True, "channel": "feishu", "messageId": "om_untrusted"}},
    {"ok": True, "channel": "other", "messageId": "om_wrong_channel"},
])
def test_external_probe_requires_successful_real_platform_receipt(tmp_path: Path, result) -> None:
    state, captured = _run_external_delivery_probe(tmp_path, result=result)
    assert captured["privilegedCalls"] == 0
    assert state["entries"]["om_external_in"]["status"] == "final_ready"
    assert state["entries"]["om_external_in"]["delivery_message_id"] == ""


def test_external_probe_retries_same_idempotency_key_without_logging_credentials(tmp_path: Path) -> None:
    state, captured = _run_external_delivery_probe(tmp_path, failure=True,
        result={"ok": True, "channel": "feishu", "messageId": "om_retry_real"})
    assert len(captured["calls"]) == 2
    assert captured["calls"][0][2]["idempotencyKey"] == captured["calls"][1][2]["idempotencyKey"]
    assert "secret-token-do-not-log" not in json.dumps(captured)
    assert state["entries"]["om_external_in"]["delivery_message_id"] == "om_retry_real"


@pytest.mark.parametrize("config", [
    {"mode": "remote", "port": 18789, "auth": {"mode": "token"}},
    {"mode": "local", "port": 18789, "auth": {"mode": "none"}},
    {"mode": "local", "port": "18789 --token unsafe", "auth": {"mode": "token"}},
])
def test_external_probe_does_not_change_auth_or_route_to_remote_gateway(tmp_path: Path, config) -> None:
    state, captured = _run_external_delivery_probe(tmp_path, config=config)
    assert captured["calls"] == [] and captured["privilegedCalls"] == 0
    assert state["entries"]["om_external_in"]["status"] == "final_ready"


def test_external_probe_missing_sdk_fails_closed_without_privileged_fallback(tmp_path: Path) -> None:
    state, captured = _run_external_delivery_probe(tmp_path, sdk_missing=True)
    assert captured["calls"] == [] and captured["privilegedCalls"] == 0
    assert "secret-token-do-not-log" not in json.dumps(captured)
    assert state["entries"]["om_external_in"]["status"] == "final_ready"


def test_tracker_correlates_out_of_order_agent_end_by_run_id(tmp_path: Path) -> None:
    state = _run_node(
        """
const plugin = require('./integrations/openclaw/eimemory-bridge/index.js').default;
const handlers = {};
plugin.register({ on(name, handler) { handlers[name] = handler; } });
const ctx = { channelId: 'feishu', conversationId: 'oc_test', sessionKey: 'agent:main:feishu:direct:ou_test' };
Promise.resolve()
  .then(() => handlers.message_received({ from: 'ou_test', messageId: 'om_run_100', runId: 'run-1' }, ctx))
  .then(() => handlers.message_received({ from: 'ou_test', messageId: 'om_run_200', runId: 'run-2' }, ctx))
  .then(() => handlers.agent_end({ success: true, runId: 'run-1', messages: [{ role: 'assistant', content: '第一条答复' }] }, ctx))
  .then(() => handlers.agent_end({ success: true, runId: 'run-2', messages: [{ role: 'assistant', content: '第二条答复' }] }, ctx));
""",
        tmp_path / "reply-state.json",
    )

    assert state["entries"]["om_run_100"]["final_text"] == "第一条答复"
    assert state["entries"]["om_run_200"]["final_text"] == "第二条答复"


def test_tracker_preserves_terminal_entry_on_duplicate_inbound(tmp_path: Path) -> None:
    state = _run_node(
        """
const plugin = require('./integrations/openclaw/eimemory-bridge/index.js').default;
const handlers = {};
plugin.register({ on(name, handler) { handlers[name] = handler; } });
const ctx = { channelId: 'feishu', conversationId: 'oc_test', sessionKey: 'agent:main:feishu:direct:ou_test' };
const inbound = { from: 'ou_test', messageId: 'om_dupe00' };
Promise.resolve()
  .then(() => handlers.message_received(inbound, ctx))
  .then(() => handlers.agent_end({ success: true, messages: [{ role: 'assistant', content: '已答复' }] }, ctx))
  .then(() => handlers.message_sent({ content: '已答复', success: true, messageId: 'om_receipt' }, ctx))
  .then(() => handlers.message_received(inbound, ctx));
""",
        tmp_path / "reply-state.json",
    )

    assert state["entries"]["om_dupe00"]["status"] == "platform_accepted"
    assert state["entries"]["om_dupe00"]["delivery_message_id"] == "om_receipt"


def test_tracker_ignores_failed_agent_end(tmp_path: Path) -> None:
    state = _run_node(
        """
const plugin = require('./integrations/openclaw/eimemory-bridge/index.js').default;
const handlers = {};
plugin.register({ on(name, handler) { handlers[name] = handler; } });
const ctx = { channelId: 'feishu', conversationId: 'oc_test', sessionKey: 'agent:main:feishu:direct:ou_test' };
Promise.resolve()
  .then(() => handlers.message_received({ from: 'ou_test', messageId: 'om_failed' }, ctx))
  .then(() => handlers.agent_end({ success: false, messages: [{ role: 'assistant', content: '部分内容' }] }, ctx));
""",
        tmp_path / "reply-state.json",
    )

    assert state["entries"]["om_failed"]["status"] == "pending"
    assert state["entries"]["om_failed"]["final_text"] == ""


def test_tracker_does_not_reuse_prior_turn_when_current_reply_is_empty(
    tmp_path: Path,
) -> None:
    state = _run_node(
        """
const plugin = require('./integrations/openclaw/eimemory-bridge/index.js').default;
const handlers = {};
plugin.register({ on(name, handler) { handlers[name] = handler; } });
const ctx = {
  channelId: 'feishu', conversationId: 'oc_test',
  sessionKey: 'agent:main:feishu:direct:ou_test'
};
Promise.resolve()
  .then(() => handlers.message_received({
    from: 'ou_test', messageId: 'om_current_turn'
  }, ctx))
  .then(() => handlers.agent_end({
    success: true,
    messages: [
      { role: 'user', content: '上一轮问题' },
      { role: 'assistant', content: '上一轮答复' },
      { role: 'user', content: '本轮问题' },
      { role: 'assistant', content: '' }
    ]
  }, ctx));
""",
        tmp_path / "reply-state.json",
    )

    entry = state["entries"]["om_current_turn"]
    assert entry["status"] == "pending"
    assert entry["final_text"] == ""


def test_tracker_state_io_failure_does_not_break_message_hook() -> None:
    env = _isolated_env()
    env["EIMEMORY_REPLY_DELIVERY_STATE_PATH"] = "/root/eimemory-invalid/reply-state.json"
    env["EIMEMORY_HOOK_COMMAND"] = "/usr/bin/true"
    env["EIMEMORY_HOOK_TIMEOUT_MS"] = "100"
    env["OPENCLAW_CONFIG_PATH"] = "/root/eimemory-invalid/openclaw.json"
    result = subprocess.run(
        [
            "node",
            "-e",
            """
const plugin = require('./integrations/openclaw/eimemory-bridge/index.js').default;
const handlers = {};
plugin.register({ on(name, handler) { handlers[name] = handler; } });
try {
  handlers.message_received({ from: 'ou_test', messageId: 'om_io0000' }, {
    channelId: 'feishu', conversationId: 'oc_test', sessionKey: 'agent:main:feishu:direct:ou_test'
  });
  setTimeout(() => process.exit(0), 100);
} catch (_error) {
  process.exit(2);
}
""",
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        env=env,
        timeout=10,
        check=False,
    )

    assert result.returncode == 0, result.stderr


def test_tracker_never_discards_active_entries_when_over_capacity(tmp_path: Path) -> None:
    state_path = tmp_path / "reply-state.json"
    state_path.write_text(
        json.dumps(
            {
                "schema_version": "openclaw_reply_delivery.v1",
                "entries": {
                    f"om_active_{index}": {
                        "inbound_message_id": f"om_active_{index}",
                        "session_key": "agent:main:feishu:direct:ou_test",
                        "received_at_ms": index,
                        "status": "pending",
                    }
                    for index in range(2_000)
                },
            }
        ),
        encoding="utf-8",
    )
    state = _run_node(
        """
const plugin = require('./integrations/openclaw/eimemory-bridge/index.js').default;
const handlers = {};
plugin.register({ on(name, handler) { handlers[name] = handler; } });
Promise.resolve(handlers.message_received({ from: 'ou_test', messageId: 'om_new000' }, {
  channelId: 'feishu', conversationId: 'oc_test', sessionKey: 'agent:main:feishu:direct:ou_test'
}));
""",
        state_path,
    )

    assert len(state["entries"]) == 2_001
    assert "om_new000" in state["entries"]


def test_tracker_reconciles_watchdog_receipt_as_single_state_writer(tmp_path: Path) -> None:
    state_path = tmp_path / "reply-state.json"
    attempts_path = tmp_path / "attempts.json"
    state_path.write_text(
        json.dumps(
            {
                "schema_version": "openclaw_reply_delivery.v1",
                "entries": {
                    "om_old000": {
                        "inbound_message_id": "om_old000",
                        "session_key": "agent:main:feishu:direct:ou_test",
                        "received_at_ms": 1,
                        "status": "answered",
                        "final_text": "旧答复",
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    attempts_path.write_text(
        json.dumps(
            {
                "entries": {
                    "om_old000": {
                        "ok": True,
                        "message_id": "om_receipt",
                        "attempted_at_ms": 2,
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    env = _isolated_env()
    env["EIMEMORY_REPLY_DELIVERY_STATE_PATH"] = str(state_path)
    env["EIMEMORY_REPLY_DELIVERY_ATTEMPTS_PATH"] = str(attempts_path)
    env["EIMEMORY_HOOK_COMMAND"] = "/usr/bin/true"
    env["OPENCLAW_CONFIG_PATH"] = str(state_path.with_name("openclaw.json"))
    result = subprocess.run(
        [
            "node",
            "-e",
            """
const plugin = require('./integrations/openclaw/eimemory-bridge/index.js').default;
const handlers = {};
plugin.register({ on(name, handler) { handlers[name] = handler; } });
Promise.resolve(handlers.message_received({ from: 'ou_test', messageId: 'om_new000' }, {
  channelId: 'feishu', conversationId: 'oc_test', sessionKey: 'agent:main:feishu:direct:ou_test'
}));
""",
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        env=env,
        timeout=10,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    state = json.loads(state_path.read_text(encoding="utf-8"))
    assert state["entries"]["om_old000"]["status"] == "platform_accepted"
    assert state["entries"]["om_old000"]["delivery_message_id"] == "om_receipt"


def test_tracker_does_not_downgrade_accepted_receipt_on_agent_end(
    tmp_path: Path,
) -> None:
    state = _run_node(
        """
const plugin = require('./integrations/openclaw/eimemory-bridge/index.js').default;
const handlers = {};
plugin.register({ on(name, handler) { handlers[name] = handler; } });
const sessionKey = 'agent:main:feishu:direct:ou_test';
const receipt = {
  ok: true,
  channel: 'feishu',
  action: 'send',
  messageId: 'om_keep_receipt',
  receipt: { primaryPlatformMessageId: 'om_keep_receipt' }
};
Promise.resolve()
  .then(() => handlers.message_received({
    from: 'ou_test', messageId: 'om_keep_inbound', runId: 'run-keep'
  }, {
    channelId: 'feishu', conversationId: 'user:ou_test', sessionKey,
    runId: 'run-keep'
  }))
  .then(() => handlers.after_tool_call({
    toolName: 'message',
    params: { action: 'send', message: 'platform accepted reply' },
    runId: 'run-keep',
    result: { content: [{ type: 'text', text: JSON.stringify(receipt) }] }
  }, {
    sessionKey,
    runId: 'run-keep',
    toolName: 'message'
  }))
  .then(() => handlers.agent_end({
    success: true,
    runId: 'run-keep',
    messages: [{ role: 'assistant', content: 'platform accepted reply\\n' }]
  }, {
    sessionKey,
    runId: 'run-keep'
  }));
""",
        tmp_path / "reply-state.json",
    )

    entry = state["entries"]["om_keep_inbound"]
    assert entry["status"] == "platform_accepted"
    assert entry["delivery_message_id"] == "om_keep_receipt"
