from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def _run_node(script: str, state_path: Path) -> dict:
    env = os.environ.copy()
    env["EIMEMORY_REPLY_DELIVERY_STATE_PATH"] = str(state_path)
    env["EIMEMORY_HOOK_COMMAND"] = "/usr/bin/true"
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
    messageId: 'om_in_1',
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
    messageId: 'om_out_1',
    sessionKey: ctx.sessionKey
  }, ctx));
""",
        tmp_path / "reply-state.json",
    )

    entry = state["entries"]["om_in_1"]
    assert entry["status"] == "delivered"
    assert entry["final_text"] == "这是最终答复"
    assert entry["delivery_message_id"] == "om_out_1"
    assert entry["conversation_id"] == "oc_test"


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
    assert entry["status"] == "answered"
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
    assert entry["status"] == "answered"
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
    assert entry["status"] == "delivered"
    assert entry["final_text"] == "tool-delivered reply"
    assert entry["delivery_message_id"] == "om_tool_receipt"


def test_tracker_ignores_group_messages(tmp_path: Path) -> None:
    state_path = tmp_path / "reply-state.json"
    state = _run_node(
        """
const plugin = require('./integrations/openclaw/eimemory-bridge/index.js').default;
const handlers = {};
plugin.register({ on(name, handler) { handlers[name] = handler; } });
Promise.resolve(handlers.message_received({
  from: 'ou_test', content: '群消息', messageId: 'om_group'
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
    from: 'ou_test', content: '测试', messageId: 'om_in_2'
  }, ctx))
  .then(() => handlers.message_sent({
    to: 'ou_test', content: '工具直接答复', success: true, messageId: 'om_out_2'
  }, ctx))
  .then(() => handlers.agent_end({
    success: true,
    messages: [{ role: 'assistant', content: '工具直接答复' }]
  }, ctx));
""",
        tmp_path / "reply-state.json",
    )

    entry = state["entries"]["om_in_2"]
    assert entry["status"] == "delivered"
    assert entry["delivery_message_id"] == "om_out_2"


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
    from: 'ou_test', content: '正常问题', messageId: 'om_in_3'
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

    entry = state["entries"]["om_in_3"]
    assert entry["status"] == "silent"
    assert entry["final_text"] == ""
    assert entry["suppress_stalled_notice"] is True


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

    assert state["entries"]["om_in_blank"]["status"] == "answered"
    assert state["entries"]["om_in_blank"]["delivery_message_id"] == ""


def test_tracker_correlates_out_of_order_agent_end_by_run_id(tmp_path: Path) -> None:
    state = _run_node(
        """
const plugin = require('./integrations/openclaw/eimemory-bridge/index.js').default;
const handlers = {};
plugin.register({ on(name, handler) { handlers[name] = handler; } });
const ctx = { channelId: 'feishu', conversationId: 'oc_test', sessionKey: 'agent:main:feishu:direct:ou_test' };
Promise.resolve()
  .then(() => handlers.message_received({ from: 'ou_test', messageId: 'om_run_1', runId: 'run-1' }, ctx))
  .then(() => handlers.message_received({ from: 'ou_test', messageId: 'om_run_2', runId: 'run-2' }, ctx))
  .then(() => handlers.agent_end({ success: true, runId: 'run-1', messages: [{ role: 'assistant', content: '第一条答复' }] }, ctx))
  .then(() => handlers.agent_end({ success: true, runId: 'run-2', messages: [{ role: 'assistant', content: '第二条答复' }] }, ctx));
""",
        tmp_path / "reply-state.json",
    )

    assert state["entries"]["om_run_1"]["final_text"] == "第一条答复"
    assert state["entries"]["om_run_2"]["final_text"] == "第二条答复"


def test_tracker_preserves_terminal_entry_on_duplicate_inbound(tmp_path: Path) -> None:
    state = _run_node(
        """
const plugin = require('./integrations/openclaw/eimemory-bridge/index.js').default;
const handlers = {};
plugin.register({ on(name, handler) { handlers[name] = handler; } });
const ctx = { channelId: 'feishu', conversationId: 'oc_test', sessionKey: 'agent:main:feishu:direct:ou_test' };
const inbound = { from: 'ou_test', messageId: 'om_dupe' };
Promise.resolve()
  .then(() => handlers.message_received(inbound, ctx))
  .then(() => handlers.agent_end({ success: true, messages: [{ role: 'assistant', content: '已答复' }] }, ctx))
  .then(() => handlers.message_sent({ content: '已答复', success: true, messageId: 'om_receipt' }, ctx))
  .then(() => handlers.message_received(inbound, ctx));
""",
        tmp_path / "reply-state.json",
    )

    assert state["entries"]["om_dupe"]["status"] == "delivered"
    assert state["entries"]["om_dupe"]["delivery_message_id"] == "om_receipt"


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


def test_tracker_state_io_failure_does_not_break_message_hook() -> None:
    env = os.environ.copy()
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
  handlers.message_received({ from: 'ou_test', messageId: 'om_io' }, {
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
Promise.resolve(handlers.message_received({ from: 'ou_test', messageId: 'om_new' }, {
  channelId: 'feishu', conversationId: 'oc_test', sessionKey: 'agent:main:feishu:direct:ou_test'
}));
""",
        state_path,
    )

    assert len(state["entries"]) == 2_001
    assert "om_new" in state["entries"]


def test_tracker_reconciles_watchdog_receipt_as_single_state_writer(tmp_path: Path) -> None:
    state_path = tmp_path / "reply-state.json"
    attempts_path = tmp_path / "attempts.json"
    state_path.write_text(
        json.dumps(
            {
                "schema_version": "openclaw_reply_delivery.v1",
                "entries": {
                    "om_old": {
                        "inbound_message_id": "om_old",
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
                    "om_old": {
                        "ok": True,
                        "message_id": "om_receipt",
                        "attempted_at_ms": 2,
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    env = os.environ.copy()
    env["EIMEMORY_REPLY_DELIVERY_STATE_PATH"] = str(state_path)
    env["EIMEMORY_REPLY_DELIVERY_ATTEMPTS_PATH"] = str(attempts_path)
    env["EIMEMORY_HOOK_COMMAND"] = "/usr/bin/true"
    result = subprocess.run(
        [
            "node",
            "-e",
            """
const plugin = require('./integrations/openclaw/eimemory-bridge/index.js').default;
const handlers = {};
plugin.register({ on(name, handler) { handlers[name] = handler; } });
Promise.resolve(handlers.message_received({ from: 'ou_test', messageId: 'om_new' }, {
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
    assert state["entries"]["om_old"]["status"] == "delivered"
    assert state["entries"]["om_old"]["delivery_message_id"] == "om_receipt"
