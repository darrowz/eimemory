from __future__ import annotations

import json
import subprocess
from pathlib import Path


def _run_node(script: str) -> dict:
    result = subprocess.run(
        ["node", "-e", script],
        cwd=Path(__file__).resolve().parents[1],
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=10,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def test_completion_gate_revises_natural_final_once() -> None:
    payload = _run_node(
        """
const plugin = require('./integrations/openclaw/eimemory-bridge/index.js').default;
const handlers = {};
plugin.register({ on(name, handler) { handlers[name] = handler; } });
Promise.resolve(handlers.before_agent_finalize({
  sessionId: 'test',
  lastAssistantMessage: '修复已经完成，但当前验证缺口：正式技能尚未应用，后面再处理。'
})).then((result) => {
  process.stdout.write(JSON.stringify(result));
});
"""
    )

    assert payload["action"] == "revise"
    assert payload["retry"]["maxAttempts"] == 1
    assert "known_fixable_issues" in payload["retry"]["instruction"]
    assert "verification_gaps" in payload["retry"]["instruction"]


def test_completion_gate_allows_clean_natural_final() -> None:
    payload = _run_node(
        """
const plugin = require('./integrations/openclaw/eimemory-bridge/index.js').default;
const handlers = {};
plugin.register({ on(name, handler) { handlers[name] = handler; } });
Promise.resolve(handlers.before_agent_finalize({
  sessionId: 'test',
  lastAssistantMessage: '修复完成，测试、配置回读和健康检查均通过。'
})).then((result) => process.stdout.write(JSON.stringify(result || {})));
"""
    )

    assert payload == {}


def test_completion_gate_blocks_unresolved_visible_send() -> None:
    payload = _run_node(
        """
const plugin = require('./integrations/openclaw/eimemory-bridge/index.js').default;
const handlers = {};
plugin.register({ on(name, handler) { handlers[name] = handler; } });
Promise.resolve(handlers.before_tool_call({
  toolName: 'message',
  params: { action: 'send', message: '修复已经完成，但当前验证缺口：正式技能尚未应用，后面再处理。' }
})).then((result) => process.stdout.write(JSON.stringify(result)));
"""
    )

    assert payload["block"] is True
    assert "继续修复" in payload["blockReason"]


def test_completion_gate_allows_diagnostic_message_with_known_gap() -> None:
    payload = _run_node(
        """
const plugin = require('./integrations/openclaw/eimemory-bridge/index.js').default;
const handlers = {};
plugin.register({ on(name, handler) { handlers[name] = handler; } });
Promise.resolve(handlers.before_tool_call({
  toolName: 'message',
  params: {
    action: 'send',
    message: '已定位原因。当前验证缺口是压缩场景尚未回归，建议按三层方案修复。'
  }
})).then((result) => process.stdout.write(JSON.stringify(result || {})));
"""
    )

    assert payload == {}


def test_completion_gate_allows_status_message_without_completion_claim() -> None:
    payload = _run_node(
        """
const plugin = require('./integrations/openclaw/eimemory-bridge/index.js').default;
const handlers = {};
plugin.register({ on(name, handler) { handlers[name] = handler; } });
Promise.resolve(handlers.before_agent_finalize({
  sessionId: 'test',
  lastAssistantMessage: '正在处理，仍有一个待修复问题，下一步继续验证。'
})).then((result) => process.stdout.write(JSON.stringify(result || {})));
"""
    )

    assert payload == {}


def test_completion_gate_allows_true_boundary_blocker() -> None:
    payload = _run_node(
        """
const plugin = require('./integrations/openclaw/eimemory-bridge/index.js').default;
const handlers = {};
plugin.register({ on(name, handler) { handlers[name] = handler; } });
Promise.resolve(handlers.before_tool_call({
  toolName: 'message',
  params: { action: 'send', message: '当前阻塞：需要用户确认付费授权后才能继续。' }
})).then((result) => process.stdout.write(JSON.stringify(result || {})));
"""
    )

    assert payload == {}


def test_completion_gate_allows_direct_feishu_message_tool_delivery() -> None:
    payload = _run_node(
        """
const plugin = require('./integrations/openclaw/eimemory-bridge/index.js').default;
const handlers = {};
plugin.register({ on(name, handler) { handlers[name] = handler; } });
Promise.resolve(handlers.before_tool_call({
  toolName: 'message',
  params: { action: 'send', message: '最终答复' }
}, {
  sessionKey: 'agent:main:feishu:direct:ou_test'
})).then((result) => process.stdout.write(JSON.stringify(result || {})));
"""
    )

    assert payload == {}


def test_completion_gate_allows_explicit_current_direct_target() -> None:
    payload = _run_node(
        """
const plugin = require('./integrations/openclaw/eimemory-bridge/index.js').default;
const handlers = {};
plugin.register({ on(name, handler) { handlers[name] = handler; } });
Promise.resolve(handlers.before_tool_call({
  toolName: 'message', params: { action: 'send', target: 'user:ou_test', message: '最终答复' }
}, { sessionKey: 'agent:main:feishu:direct:ou_test', conversationId: 'oc_test' }))
  .then((result) => process.stdout.write(JSON.stringify(result || {})));
"""
    )

    assert payload == {}


def test_completion_gate_allows_delivery_reporter_bypass() -> None:
    payload = _run_node(
        """
const plugin = require('./integrations/openclaw/eimemory-bridge/index.js').default;
const handlers = {};
plugin.register({ on(name, handler) { handlers[name] = handler; } });
Promise.resolve(handlers.before_tool_call({
  toolName: 'message',
  params: { action: 'send', target: 'user:ou_test', kind: 'reply_delivery_reporter', message: '部署回执' }
}, { sessionKey: 'agent:main:feishu:direct:ou_test', conversationId: 'oc_test' }))
  .then((result) => process.stdout.write(JSON.stringify(result || {})));
"""
    )

    assert payload == {}
