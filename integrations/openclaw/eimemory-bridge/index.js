'use strict';

const { spawnSync } = require('node:child_process');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');

const HONGTU_AGENT_ID = 'hongtu';
const HONGTU_WORKSPACE_ID = 'embodied';
const DEFAULT_OPERATOR_USER_ID = 'darrow';
const DEFAULT_RECALL_MODE = 'fast';
const DEFAULT_RECALL_BUDGET_MS = 800;
const DEFAULT_FAST_CANDIDATE_LIMIT = 24;
const DEFAULT_HOOK_CACHE_TTL_MS = 10000;
const DEFAULT_HOOK_TIMEOUT_MS = 8000;
const DEFAULT_TERMINAL_HOOK_TIMEOUT_MS = 30000;
const DEFAULT_BRIDGE_TIMEOUT_MS = 8000;
const LOOP_TASK_CORRELATION_TTL_MS = 2 * 60 * 60 * 1000;
const COMPLETION_GATE_RETRY_KEY = 'eimemory-completion-gate-v1';
const UNRESOLVED_COMPLETION_MARKERS = [
  /(?:当前|仍有|剩余|存在|还有).{0,12}(?:验证缺口|系统性缺口|已知问题|待修复|未完成)/i,
  /(?:验证缺口|known_fixable_issues|verification_gaps).{0,24}(?:尚未|未清零|非零|待|后面|以后|later|pending)/i,
  /(?:尚未|还没|未能).{0,18}(?:应用|部署|修复|验证|完成)/i,
  /(?:后面|以后|稍后|下次|later).{0,12}(?:再|处理|修|验证|完成)/i,
];
const TRUE_BOUNDARY_MARKERS = [
  /需要(?:用户|鸿哥|曾总).{0,12}(?:确认|决定|授权|提供|操作)/i,
  /(?:付费|费用|购买|充值|资金|授权|权限|密钥|凭据|不可逆|删除|发布|外发).{0,18}(?:确认|决定|批准|提供|操作|边界)/i,
  /(?:external|外部).{0,12}(?:state|状态|coordination|协调|权限|授权)/i,
];
const REGISTRATION_STATE_KEY = Symbol.for('eimemory.bridge.registrationState');
const VISION_BRIDGE_QUERY_MARKERS = [
  '看到了什么',
  '现在看到',
  '视觉',
  '摄像头',
  '画面',
  '有没有人',
  '有人吗',
  '茶室',
  'what do you see',
  'camera',
  'vision',
  'scene',
];

function registrationState() {
  if (!globalThis[REGISTRATION_STATE_KEY]) {
    globalThis[REGISTRATION_STATE_KEY] = {
      hookRegistrations: new WeakMap(),
      fallbackHookNames: new Set(),
      globalHookNames: new Set(),
    };
  }
  return globalThis[REGISTRATION_STATE_KEY];
}

const hookResultCache = new Map();
const pendingLoopTasks = new Map();

function nowMs() {
  return Date.now();
}

function pruneHookCache() {
  const cutoff = nowMs() - DEFAULT_HOOK_CACHE_TTL_MS;
  for (const [key, entry] of hookResultCache.entries()) {
    if (!entry || entry.createdAt < cutoff) {
      hookResultCache.delete(key);
    }
  }
}

function mergeHookEventContext(event, context) {
  const merged = { ...normalizeObject(context) };
  for (const [key, value] of Object.entries(normalizeObject(event))) {
    if (value !== undefined && value !== null && value !== '') {
      merged[key] = value;
    }
  }
  return merged;
}

function loopCorrelationKeys(event) {
  const candidates = [
    ['run', event?.runId || event?.run_id],
    ['job', event?.jobId || event?.job_id],
    ['turn', event?.turnId || event?.turn_id],
    ['request', event?.requestId || event?.request_id],
    ['trace', event?.traceId || event?.trace_id || event?.trace?.id],
    ['task', event?.taskId || event?.task_id],
    ['event', event?.eventId || event?.event_id || event?.id],
    ['message', event?.messageId || event?.message_id || event?.message?.id],
    ['session-key', event?.sessionKey || event?.session_key],
    ['session', normalizeSessionId(event)],
  ];
  const keys = [];
  for (const [kind, raw] of candidates) {
    const value = String(raw || '').trim();
    if (value) {
      keys.push(`${kind}:${value}`);
    }
  }
  return [...new Set(keys)];
}

function prunePendingLoopTasks() {
  const cutoff = nowMs() - LOOP_TASK_CORRELATION_TTL_MS;
  for (const [key, entry] of pendingLoopTasks.entries()) {
    if (!entry || entry.createdAt < cutoff) {
      pendingLoopTasks.delete(key);
    }
  }
}

function rememberLoopTask(event, payload) {
  const taskId = String(payload?.task_context?.openclaw_loop_task_id || '').trim();
  if (!taskId) {
    return;
  }
  prunePendingLoopTasks();
  const keys = loopCorrelationKeys(event);
  const entry = { taskId, createdAt: nowMs(), keys };
  for (const key of keys) {
    pendingLoopTasks.set(key, entry);
  }
}

function correlatePendingLoopTask(event) {
  const rawContext = normalizeObject(event?.task_context || event?.taskContext);
  if (String(rawContext.openclaw_loop_task_id || '').trim()) {
    return event;
  }
  prunePendingLoopTasks();
  let entry = null;
  for (const key of loopCorrelationKeys(event)) {
    entry = pendingLoopTasks.get(key) || null;
    if (entry) {
      break;
    }
  }
  if (!entry) {
    return event;
  }
  const taskContext = { ...rawContext, openclaw_loop_task_id: entry.taskId };
  return { ...event, task_context: taskContext, taskContext };
}

function forgetTerminalLoopTask(event, result) {
  const status = String(result?.loop_task?.status || '').trim().toLowerCase();
  if (!['done', 'failed', 'rolled_back'].includes(status)) {
    return;
  }
  const taskId = String(result?.loop_task?.task_id || result?.loop_task?.id || '').trim();
  for (const [key, entry] of pendingLoopTasks.entries()) {
    if ((taskId && entry?.taskId === taskId) || loopCorrelationKeys(event).includes(key)) {
      pendingLoopTasks.delete(key);
    }
  }
}

function stableJson(value) {
  if (value == null || typeof value !== 'object') {
    return JSON.stringify(value);
  }
  if (Array.isArray(value)) {
    return `[${value.map((item) => stableJson(item)).join(',')}]`;
  }
  return `{${Object.keys(value).sort().map((key) => `${JSON.stringify(key)}:${stableJson(value[key])}`).join(',')}}`;
}

function cacheKeyFor(kind, hook, payload) {
  return `${kind}:${hook}:${stableJson(payload)}`;
}

function normalizeTraceFields(event) {
  return {
    run_id: String(event?.run_id || event?.runId || ''),
    event_id: String(event?.event_id || event?.eventId || event?.id || ''),
    message_id: String(event?.message_id || event?.messageId || event?.message?.id || event?.message?.messageId || ''),
    trace_id: String(event?.trace_id || event?.traceId || event?.trace?.id || ''),
    idempotency_key: String(event?.idempotency_key || event?.idempotencyKey || ''),
    task_id: String(event?.task_id || event?.taskId || ''),
    turn_id: String(event?.turn_id || event?.turnId || ''),
    request_id: String(event?.request_id || event?.requestId || ''),
    started_at: String(event?.started_at || event?.startedAt || ''),
    attempt: String(event?.attempt || event?.attempt_id || event?.attemptId || ''),
  };
}

function resolveTransportLedgerPath() {
  const configured = (process.env.EIMEMORY_BRIDGE_TRANSPORT_LEDGER || '').trim();
  if (configured) {
    return configured;
  }
  const root = (process.env.EIMEMORY_ROOT || '').trim();
  if (root) {
    return path.join(root, 'openclaw_bridge_transport_failures.jsonl');
  }
  return path.join(os.tmpdir(), 'eimemory-openclaw-bridge-transport-failures.jsonl');
}

function serializeTransportError(error) {
  return {
    name: String(error?.name || ''),
    message: String(error?.message || error || ''),
    code: String(error?.code || ''),
    errno: String(error?.errno || ''),
    syscall: String(error?.syscall || ''),
    path: String(error?.path || ''),
  };
}

function recordTransportFailure(details) {
  try {
    const ledgerPath = resolveTransportLedgerPath();
    fs.mkdirSync(path.dirname(ledgerPath), { recursive: true });
    fs.appendFileSync(
      ledgerPath,
      `${JSON.stringify({
        event_type: 'openclaw.bridge.transport_error',
        observed_at: new Date().toISOString(),
        ...details,
      })}\n`,
      'utf-8',
    );
    return ledgerPath;
  } catch (_) {
    return '';
  }
}

function splitCommand(command) {
  const parts = [];
  let current = '';
  let quote = '';
  for (let index = 0; index < command.length; index += 1) {
    const char = command[index];
    if (quote) {
      if (char === quote) {
        quote = '';
      } else if (
        char === '\\'
        && index + 1 < command.length
        && (command[index + 1] === quote || command[index + 1] === '\\')
      ) {
        index += 1;
        current += command[index];
      } else {
        current += char;
      }
      continue;
    }
    if (char === '"' || char === "'") {
      quote = char;
      continue;
    }
    if (/\s/.test(char)) {
      if (current) {
        parts.push(current);
        current = '';
      }
      continue;
    }
    current += char;
  }
  if (current) {
    parts.push(current);
  }
  return parts;
}

function resolveHookCommand() {
  const configured = (process.env.EIMEMORY_HOOK_COMMAND || '').trim();
  if (configured) {
    return splitCommand(configured);
  }
  return ['eimemory', 'openclaw-hook'];
}

function resolveBridgeCommand() {
  const configured = (process.env.EIMEMORY_BRIDGE_COMMAND || '').trim();
  if (configured) {
    return splitCommand(configured);
  }
  return ['eimemory', 'ei-bridge', 'feishu'];
}

function resolveCliCommand() {
  const configured = (process.env.EIMEMORY_CLI_COMMAND || '').trim();
  if (configured) {
    return splitCommand(configured);
  }
  const hookCommand = resolveHookCommand();
  if (hookCommand[hookCommand.length - 1] === 'openclaw-hook') {
    return hookCommand.slice(0, -1);
  }
  return ['eimemory'];
}

function normalizeEventPayload(hook, event) {
  if (hook === 'message_received') {
    const scope = normalizeScope(event);
    const content = normalizeContent(event?.content ?? event?.message?.content);
    const role = normalizeMessageRole(event);
      return {
        session_id: String(event?.sessionId || event?.session_id || ''),
        ...scope,
        ...normalizeTraceFields(event),
        capture_memory: Boolean(event?.capture_memory || event?.captureMemory),
        message: {
          role,
          content,
      },
    };
  }
  if (hook === 'before_prompt_build') {
    const rawQuery = String(event?.query || event?.prompt || '');
    const promptMetadata = extractPromptMetadata(rawQuery);
    const scope = normalizeScope(event, promptMetadata);
    return {
      session_id: normalizeSessionId(event, promptMetadata),
      ...scope,
      ...normalizeTraceFields(event),
      query: cleanPromptQuery(rawQuery),
      raw_query: rawQuery,
      task_context: normalizeRecallContext(event?.task_context || event?.taskContext || {}),
    };
  }
  const scope = normalizeScope(event);
  const rawQuery = String(event?.query || event?.prompt || event?.userPhrase || event?.user_phrase || '');
  const taskContext = normalizeRecallContext(event?.task_context || event?.taskContext || {});
  const outcome = normalizeObject(event?.outcome);
  const verifiedState = normalizeVerifiedState(event, outcome);
  return {
    session_id: normalizeSessionId(event),
    ...scope,
    ...normalizeTraceFields(event),
    query: cleanPromptQuery(rawQuery),
    raw_query: rawQuery,
    task_context: taskContext,
    user_messages: normalizeRoleMessages(event, 'user'),
    assistant_messages: normalizeRoleMessages(event, 'assistant'),
    physical_conditions: normalizeObject(event?.physical_conditions || event?.physicalConditions),
    environment: normalizeObject(event?.environment),
    tools: normalizeStringList(event?.tools || event?.used_tools || event?.usedTools),
    action_path: normalizeStringList(event?.action_path || event?.actionPath || event?.execution_path || event?.executionPath),
    outcome: {
      success: Object.prototype.hasOwnProperty.call(outcome, 'success') ? outcome.success !== false : event?.success !== false,
      notes: String(outcome.notes || outcome.reason || event?.error || ''),
      ...(verifiedState.present ? { verified: verifiedState.value } : {}),
      verification: String(
        outcome.verification
        || outcome.verification_method
        || outcome.verificationMethod
        || event?.verification
        || event?.verification_method
        || event?.verificationMethod
        || taskContext.verification
        || ''
      ),
      correction_from_user: String(
        outcome.correction_from_user
        || outcome.correctionFromUser
        || outcome.correction
        || event?.correction_from_user
        || event?.correctionFromUser
        || event?.user_feedback
        || event?.userFeedback
        || ''
      ),
    },
  };
}

function normalizeRoleMessages(event, role) {
  const roleName = String(role || '').toLowerCase();
  const explicitKeys = roleName === 'user'
    ? ['user_messages', 'userMessages']
    : ['assistant_messages', 'assistantMessages'];
  const messages = [];
  for (const key of explicitKeys) {
    const raw = event?.[key];
    if (!Array.isArray(raw)) {
      continue;
    }
    for (const message of raw) {
      messages.push({ content: normalizeContent(typeof message === 'object' ? message?.content : message) });
    }
  }
  if (Array.isArray(event?.messages)) {
    for (const message of event.messages) {
      if (String(message?.role || '').toLowerCase() === roleName) {
        messages.push({ content: normalizeContent(message?.content) });
      }
    }
  }
  return messages.filter((message) => message.content);
}

function normalizeStringList(value) {
  if (value == null) {
    return [];
  }
  const raw = Array.isArray(value) ? value : [value];
  const seen = new Set();
  const items = [];
  for (const item of raw) {
    const text = normalizeContent(item).trim();
    if (!text || seen.has(text)) {
      continue;
    }
    seen.add(text);
    items.push(text);
  }
  return items;
}

function normalizeObject(value) {
  if (!value || typeof value !== 'object' || Array.isArray(value)) {
    return {};
  }
  return Object.assign({}, value);
}

function normalizeVerifiedState(event, outcome) {
  const candidates = [
    [outcome, 'verified'],
    [outcome, 'is_verified'],
    [outcome, 'isVerified'],
    [event, 'verified'],
    [event, 'is_verified'],
    [event, 'isVerified'],
    [event, 'verification_status'],
  ];
  for (const [container, key] of candidates) {
    if (!container || !Object.prototype.hasOwnProperty.call(container, key)) {
      continue;
    }
    const value = container[key];
    if (key !== 'verification_status' || typeof value !== 'string') {
      return { present: true, value };
    }
    const normalized = value.trim().toLowerCase();
    if (['verified', 'passed', 'success', 'true'].includes(normalized)) {
      return { present: true, value: true };
    }
    if (['unverified', 'failed', 'failure', 'false'].includes(normalized)) {
      return { present: true, value: false };
    }
    return { present: true, value };
  }
  return { present: false, value: undefined };
}

function normalizeRecallContext(rawContext) {
  const context = Object.assign({}, rawContext || {});
  let recallMode = String(context.recall_mode || '').trim().toLowerCase();
  if (recallMode === 'deep') {
    recallMode = 'raw_hybrid';
  } else if (recallMode !== 'raw_hybrid') {
    recallMode = DEFAULT_RECALL_MODE;
  }
  context.recall_mode = recallMode;
  const budget = Number.parseInt(context.recall_budget_ms, 10);
  context.recall_budget_ms = Number.isFinite(budget) && budget > 0 ? budget : DEFAULT_RECALL_BUDGET_MS;
  if (recallMode === 'fast') {
    const candidateLimit = Number.parseInt(context.candidate_limit, 10);
    if (!Number.isFinite(candidateLimit)) {
      context.candidate_limit = DEFAULT_FAST_CANDIDATE_LIMIT;
    } else {
      context.candidate_limit = Math.max(24, Math.min(360, candidateLimit));
    }
  }
  return context;
}

function normalizeSessionId(event, metadata = {}) {
  const explicit = String(event?.sessionId || event?.session_id || '').trim();
  if (explicit) {
    return explicit;
  }
  const chatId = String(metadata.chat_id || '').trim();
  if (chatId) {
    return `feishu:${chatId}`;
  }
  const senderId = String(metadata.sender_id || metadata.sender || '').trim();
  if (senderId) {
    return `feishu:user:${senderId}`;
  }
  return '';
}

function normalizeMessageRole(event) {
  const candidates = [event?.message?.role, event?.role, event?.from];
  for (const candidate of candidates) {
    if (typeof candidate === 'string' && candidate.trim()) {
      return candidate.trim();
    }
  }
  return 'user';
}

function normalizeScope(event, metadata = {}) {
  const sessionUserId = userIdFromSession(event?.sessionId || event?.session_id || '');
  return {
    tenant_id: String(event?.tenantId || event?.tenant_id || 'default'),
    agent_id: String(
      event?.agentId
      || event?.agent_id
      || process.env.EIMEMORY_AGENT_ID
      || HONGTU_AGENT_ID
    ),
    workspace_id: String(
      event?.workspaceId
      || event?.workspace_id
      || process.env.EIMEMORY_WORKSPACE_ID
      || process.env.EIMEMORY_NODE
      || HONGTU_WORKSPACE_ID
    ),
    user_id: String(
      event?.userId
      || event?.user_id
      || event?.senderId
      || event?.sender_id
      || metadata.sender_id
      || metadata.sender
      || sessionUserId
      || DEFAULT_OPERATOR_USER_ID
    ),
    preserve_scope: process.env.EIMEMORY_PRESERVE_SCOPE === '1'
      || process.env.EIMEMORY_PRESERVE_SCOPE === 'true',
  };
}

function userIdFromSession(sessionId) {
  const session = String(sessionId || '').trim();
  if (!session) {
    return '';
  }
  const directMatch = session.match(/feishu:direct:([^:]+)$/i);
  if (directMatch) {
    return directMatch[1];
  }
  const userMatch = session.match(/feishu:user:([^:]+)$/i);
  if (userMatch) {
    return userMatch[1];
  }
  return '';
}

function extractPromptMetadata(prompt) {
  const merged = {};
  const text = String(prompt || '');
  const metadataSection = extractTrustedMetadataSection(text);
  const conversation = extractLabeledJsonFence(metadataSection, 'Conversation info');
  if (conversation) {
    Object.assign(merged, conversation);
  }
  const sender = extractLabeledJsonFence(metadataSection, 'Sender');
  if (sender) {
    const senderId = sender.sender_id || sender.sender || sender.id || sender.open_id || sender.user_id;
    if (senderId && !merged.sender_id) {
      merged.sender_id = senderId;
    }
    if (sender.name && !merged.sender_name) {
      merged.sender_name = sender.name;
    }
  }
  const messageMatch = text.match(/\[msg:([^\]]+)\]/i);
  if (messageMatch && !merged.message_id) {
    merged.message_id = messageMatch[1];
  }
  return merged;
}

function extractTrustedMetadataSection(text) {
  const lines = String(text || '').split(/\r?\n/);
  const trusted = [];
  let started = false;
  let inFence = false;
  for (const rawLine of lines) {
    const line = String(rawLine || '');
    const trimmed = line.trim();
    const lowered = trimmed.toLowerCase();
    const isWrapperLine = (
      !trimmed
      || lowered.startsWith('system:')
      || lowered.startsWith('conversation info')
      || lowered.startsWith('sender ')
      || lowered.startsWith('sender(')
      || lowered.startsWith('sender:')
      || trimmed.startsWith('```')
      || (started && inFence)
    );
    if (!started && !isWrapperLine) {
      break;
    }
    if (isWrapperLine) {
      trusted.push(line);
      started = true;
    } else if (started) {
      break;
    }
    if (trimmed.startsWith('```')) {
      inFence = !inFence;
    }
  }
  return trusted.join('\n');
}

function extractLabeledJsonFence(text, label) {
  const escaped = label.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
  const pattern = new RegExp(
    `(?:^|\\r?\\n)\\s*${escaped}\\b[^\\r\\n]*\\r?\\n\\s*\\\`\\\`\\\`(?:json)?\\s*([\\s\\S]*?)\\\`\\\`\\\``,
    'i'
  );
  const match = pattern.exec(text);
  if (!match) {
    return null;
  }
  try {
    const parsed = JSON.parse(match[1]);
    if (parsed && typeof parsed === 'object' && !Array.isArray(parsed)) {
      return parsed;
    }
  } catch (_error) {
    // Metadata is best-effort; malformed prompt wrappers should never block recall.
  }
  return null;
}

function cleanPromptQuery(query) {
  const text = String(query || '').trim();
  if (!text) {
    return '';
  }
  const withoutFences = text.replace(/```(?:json)?\s*[\s\S]*?```/gi, '');
  const lines = withoutFences
    .split(/\r?\n/)
    .map((line) => line.trim())
    .filter((line) => {
      if (!line) {
        return true;
      }
      const lowered = line.toLowerCase();
      if (
        lowered.startsWith('system:')
        || lowered.startsWith('conversation info')
        || lowered.startsWith('sender ')
        || lowered.startsWith('sender(')
        || lowered.startsWith('sender:')
      ) {
        return false;
      }
      if (line.startsWith('{') && line.endsWith('}')) {
        return false;
      }
      return true;
    });
  const paragraphs = lines
    .join('\n')
    .split(/\n{2,}/)
    .map((paragraph) => paragraph.replace(/\s+/g, ' ').trim())
    .filter(Boolean);
  if (paragraphs.length) {
    return paragraphs[paragraphs.length - 1];
  }
  return lines.filter(Boolean).join(' ').trim();
}

function normalizeContent(content) {
  if (content == null) {
    return '';
  }
  if (typeof content === 'string') {
    return content;
  }
  if (Array.isArray(content)) {
    return content
      .map((item) => normalizeContent(item))
      .filter(Boolean)
      .join('\n')
      .trim();
  }
  if (typeof content === 'object') {
    if (typeof content.text === 'string') {
      return content.text;
    }
    if (content.content != null) {
      return normalizeContent(content.content);
    }
    try {
      return JSON.stringify(content);
    } catch (_error) {
      return String(content);
    }
  }
  return String(content);
}

function invokeHook(api, hook, event) {
  const payload = normalizeEventPayload(hook, event);
  const key = cacheKeyFor('hook', hook, payload);
  const cacheable = hook === 'before_prompt_build';
  if (cacheable) {
    pruneHookCache();
    const cached = hookResultCache.get(key);
    if (cached) {
      return cached.value;
    }
  }
  const command = resolveHookCommand();
  const result = spawnSync(command[0], [...command.slice(1), hook], {
    input: JSON.stringify(payload),
    encoding: 'utf-8',
    timeout: configuredHookTimeout(api, hook, defaultHookTimeoutMs(hook)),
  });
  if (result.error) {
    throw result.error;
  }
  if (result.status !== 0) {
    throw new Error(result.stderr || result.stdout || `eimemory hook ${hook} failed`);
  }
  const parsed = JSON.parse(result.stdout || '{}');
  if (cacheable) {
    hookResultCache.set(key, { createdAt: nowMs(), value: parsed });
  }
  return parsed;
}

function invokeBridge(api, event) {
  const key = cacheKeyFor('bridge', 'feishu', event);
  pruneHookCache();
  const cached = hookResultCache.get(key);
  if (cached) {
    return cached.value;
  }
  const command = resolveBridgeCommand();
  const result = spawnSync(command[0], [...command.slice(1)], {
    input: JSON.stringify(event),
    encoding: 'utf-8',
    timeout: configuredBridgeTimeout(api, DEFAULT_BRIDGE_TIMEOUT_MS),
  });
  if (result.error) {
    throw result.error;
  }
  if (result.status !== 0) {
    throw new Error(result.stderr || result.stdout || 'ei-bridge feishu failed');
  }
  const parsed = JSON.parse(result.stdout || '{}');
  hookResultCache.set(key, { createdAt: nowMs(), value: parsed });
  return parsed;
}

function invokeCli(args) {
  const command = resolveCliCommand();
  const result = spawnSync(command[0], [...command.slice(1), ...args], {
    encoding: 'utf-8',
    timeout: Number(process.env.EIMEMORY_TOOL_TIMEOUT_MS || 30000),
  });
  if (result.error) {
    throw result.error;
  }
  if (result.status !== 0) {
    throw new Error(result.stderr || result.stdout || `eimemory cli failed: ${args.join(' ')}`);
  }
  return JSON.parse(result.stdout || '{}');
}

function safeInvokeHook(api, hook, event) {
  try {
    const result = invokeHook(api, hook, event);
    api?.logger?.info?.(`eimemory-bridge: ${hook} completed`);
    return result;
  } catch (error) {
    const ledgerPath = recordTransportFailure({
      transport: 'hook',
      hook,
      command: resolveHookCommand(),
      error: serializeTransportError(error),
    });
    api?.logger?.warn?.(`eimemory-bridge: ${hook} failed: ${error?.message || String(error)}`);
    if (ledgerPath) {
      api?.logger?.warn?.(`eimemory-bridge: transport failure recorded at ${ledgerPath}`);
    }
    return null;
  }
}

function safeInvokeBridge(api, event) {
  try {
    const result = invokeBridge(api, event);
    api?.logger?.info?.('eimemory-bridge: ei-bridge feishu completed');
    return result;
  } catch (error) {
    const ledgerPath = recordTransportFailure({
      transport: 'bridge',
      hook: 'ei-bridge feishu',
      command: resolveBridgeCommand(),
      error: serializeTransportError(error),
    });
    api?.logger?.warn?.(`eimemory-bridge: ei-bridge feishu failed: ${error?.message || String(error)}`);
    if (ledgerPath) {
      api?.logger?.warn?.(`eimemory-bridge: transport failure recorded at ${ledgerPath}`);
    }
    return null;
  }
}

function registerTypedHook(api, name, handler) {
  if (api?.hooks?.on) {
    api.hooks.on(name, handler);
    return;
  }
  if (api?.on) {
    api.on(name, handler);
  }
}

function hookRegistrationTarget(api) {
  if (api?.hooks && (typeof api.hooks === 'object' || typeof api.hooks === 'function')) {
    return api.hooks;
  }
  if (api && (typeof api === 'object' || typeof api === 'function')) {
    return api;
  }
  return null;
}

function registerTypedHookOnce(api, name, handler) {
  const state = registrationState();
  if (state.globalHookNames.has(name)) {
    api?.logger?.debug?.(`eimemory-bridge: ${name} hook already registered`);
    return;
  }
  const target = hookRegistrationTarget(api);
  if (!target) {
    if (state.fallbackHookNames.has(name)) {
      api?.logger?.debug?.(`eimemory-bridge: ${name} hook already registered`);
      return;
    }
    registerTypedHook(api, name, handler);
    state.fallbackHookNames.add(name);
    state.globalHookNames.add(name);
    return;
  }
  let names = state.hookRegistrations.get(target);
  if (!names) {
    names = new Set();
    state.hookRegistrations.set(target, names);
  }
  if (names.has(name)) {
    api?.logger?.debug?.(`eimemory-bridge: ${name} hook already registered`);
    return;
  }
  registerTypedHook(api, name, handler);
  names.add(name);
  state.globalHookNames.add(name);
}

function truthy(value) {
  return /^(1|true|yes|on)$/i.test(String(value || '').trim());
}

function positiveIntEnv(name, fallback) {
  const parsed = Number(process.env[name] || 0);
  return Number.isFinite(parsed) && parsed > 0 ? parsed : fallback;
}

function hasUnresolvedCompletion(text) {
  const normalized = String(text || '').trim();
  if (!normalized || TRUE_BOUNDARY_MARKERS.some((pattern) => pattern.test(normalized))) {
    return false;
  }
  return UNRESOLVED_COMPLETION_MARKERS.some((pattern) => pattern.test(normalized));
}

function completionGateRevision(event) {
  if (!hasUnresolvedCompletion(event?.lastAssistantMessage)) {
    return undefined;
  }
  return {
    action: 'revise',
    reason: 'Completion Gate: answer still reports a safe, fixable unresolved issue.',
    retry: {
      instruction: 'Do not finalize yet. Continue safe in-scope work until known_fixable_issues = 0 and verification_gaps = 0. Only stop for a real user-confirmation, cost, authorization, irreversible-action, external-coordination, or external-state boundary.',
      idempotencyKey: COMPLETION_GATE_RETRY_KEY,
      maxAttempts: 1,
    },
  };
}

function completionGateBeforeToolCall(event) {
  if (String(event?.toolName || '') !== 'message') {
    return undefined;
  }
  const params = event?.params || {};
  if (String(params.action || '') !== 'send' || !hasUnresolvedCompletion(params.message)) {
    return undefined;
  }
  return {
    block: true,
    blockReason: 'Completion Gate：消息仍包含可安全修复的已知缺口。继续修复并完成验证后再发送；只有真实授权、费用、不可逆操作、外部协调或外部状态阻塞可以停止。',
  };
}

function positiveIntValue(value, fallback) {
  const parsed = Number(value || 0);
  return Number.isFinite(parsed) && parsed > 0 ? parsed : fallback;
}

function defaultHookTimeoutMs(hook) {
  return hook === 'agent_end' || hook === 'session_end'
    ? DEFAULT_TERMINAL_HOOK_TIMEOUT_MS
    : DEFAULT_HOOK_TIMEOUT_MS;
}

function readOpenClawBridgeHooksConfig() {
  try {
    const configPath = process.env.OPENCLAW_CONFIG_PATH
      || path.join(process.env.OPENCLAW_STATE_DIR || path.join(os.homedir(), '.openclaw'), 'openclaw.json');
    const config = JSON.parse(fs.readFileSync(configPath, 'utf8'));
    return config?.plugins?.entries?.['eimemory-bridge']?.hooks || {};
  } catch {
    return {};
  }
}

function configuredHookTimeout(api, hook, fallback) {
  const envTimeout = positiveIntEnv('EIMEMORY_HOOK_TIMEOUT_MS', fallback);
  const config = api?.config || {};
  const hookPolicy = api?.hookPolicy || api?.hooksPolicy || config.hooks || {};
  const filePolicy = readOpenClawBridgeHooksConfig();
  return positiveIntValue(
    hookPolicy?.timeouts?.[hook]
      ?? config?.timeouts?.[hook]
      ?? filePolicy?.timeouts?.[hook],
    envTimeout
  );
}

function configuredBridgeTimeout(api, fallback) {
  const envTimeout = positiveIntEnv('EIMEMORY_BRIDGE_TIMEOUT_MS', fallback);
  const config = api?.config || {};
  const hookPolicy = api?.hookPolicy || api?.hooksPolicy || config.hooks || {};
  const filePolicy = readOpenClawBridgeHooksConfig();
  return positiveIntValue(
    hookPolicy?.timeouts?.bridge
      ?? hookPolicy?.timeouts?.feishu_bridge
      ?? config?.timeouts?.bridge
      ?? config?.timeouts?.feishu_bridge
      ?? filePolicy?.timeouts?.bridge
      ?? filePolicy?.timeouts?.feishu_bridge,
    envTimeout
  );
}

function readOpenClawPromptInjectionPolicy() {
  try {
    return readOpenClawBridgeHooksConfig()?.allowPromptInjection === true;
  } catch {
    return false;
  }
}

function promptInjectionAllowed(api) {
  const config = api?.config || {};
  const hookPolicy = api?.hookPolicy || api?.hooksPolicy || config.hooks || {};
  return hookPolicy.allowPromptInjection === true
    || config.allowPromptInjection === true
    || config.allow_prompt_injection === true
    || readOpenClawPromptInjectionPolicy();
}

function promptInjectionEnabled(api) {
  return truthy(process.env.EIMEMORY_ENABLE_PROMPT_INJECTION) && promptInjectionAllowed(api);
}

function promptBridgeEnabled(api) {
  const config = api?.config || {};
  return config.enableFeishuBridgePrompt === true
    || config.enable_feishu_bridge_prompt === true
    || config.enablePromptBridge === true
    || config.enable_prompt_bridge === true
    || truthy(process.env.EIMEMORY_ENABLE_FEISHU_BRIDGE_PROMPT)
    || truthy(process.env.EIMEMORY_ENABLE_PROMPT_BRIDGE);
}

function shouldInvokeBridgeBeforePrompt(api, event) {
  if (promptBridgeEnabled(api)) {
    return true;
  }
  const query = String(
    event?.query
    || event?.prompt
    || event?.text
    || event?.content
    || event?.message?.content
    || ''
  ).toLowerCase();
  return VISION_BRIDGE_QUERY_MARKERS.some((marker) => query.includes(marker));
}

function memoryE2EToolEnabled(api) {
  const config = api?.config || {};
  return config.enableMemoryE2ECheck === true
    || config.enable_memory_e2e_check === true
    || truthy(process.env.EIMEMORY_ENABLE_MEMORY_E2E_TOOL);
}

function usesLegacyHookApi(api) {
  return Boolean(api?.on && !api?.hooks?.on);
}

function registerStatusTool(api) {
  if (!api?.registerTool) {
    return;
  }
  api.registerTool(() => ({
    name: 'eimemory_bridge_status',
    label: 'eimemory Bridge Status',
    description: 'Report whether the OpenClaw eimemory bridge commands are configured.',
    parameters: {
      type: 'object',
      additionalProperties: false,
      properties: {},
      required: [],
    },
    async execute() {
      const status = {
        ok: true,
        hookCommandConfigured: Boolean((process.env.EIMEMORY_HOOK_COMMAND || '').trim()),
        bridgeCommandConfigured: Boolean((process.env.EIMEMORY_BRIDGE_COMMAND || '').trim()),
        promptInjectionEnabled: promptInjectionEnabled(api),
        promptInjectionEnvEnabled: truthy(process.env.EIMEMORY_ENABLE_PROMPT_INJECTION),
        allowPromptInjection: promptInjectionAllowed(api),
        promptBridgeEnabled: promptBridgeEnabled(api),
      };
      return {
        content: [{
          type: 'text',
          text: JSON.stringify(status),
        }],
        details: status,
      };
    },
  }), { name: 'eimemory_bridge_status' });
}

function registerMemoryE2ETool(api) {
  if (!api?.registerTool || !memoryE2EToolEnabled(api)) {
    return;
  }
  api.registerTool(() => ({
    name: 'memory_e2e_check',
    label: 'eimemory E2E Check',
    description: 'Run an eimemory OpenClaw end-to-end memory check.',
    parameters: {
      type: 'object',
      additionalProperties: false,
      properties: {
        query: { type: 'string' },
        agent_id: { type: 'string' },
        workspace_id: { type: 'string' },
        user_id: { type: 'string' },
      },
      required: [],
    },
    async execute(input = {}) {
      const args = ['eval', 'openclaw-e2e'];
      if (input.query) {
        args.push('--query', String(input.query));
      }
      if (input.agent_id) {
        args.push('--scope-agent', String(input.agent_id));
      }
      if (input.workspace_id) {
        args.push('--scope-workspace', String(input.workspace_id));
      }
      if (input.user_id) {
        args.push('--scope-user', String(input.user_id));
      }
      let result;
      try {
        result = invokeCli(args);
      } catch (error) {
        const command = resolveCliCommand();
        const ledgerPath = recordTransportFailure({
          transport: 'tool',
          hook: 'memory_e2e_check',
          command: [...command, ...args],
          error: serializeTransportError(error),
        });
        result = {
          ok: false,
          error: 'transport_error',
          detail: String(error?.message || error || ''),
          transport_ledger_path: ledgerPath,
        };
      }
      return {
        content: [{
          type: 'text',
          text: JSON.stringify(result),
        }],
        details: result,
      };
    },
  }), { name: 'memory_e2e_check' });
}

module.exports.default = {
  id: 'eimemory-bridge',
  name: 'eimemory Bridge',
  description: 'Forwards OpenClaw lifecycle hooks into eimemory.',
  configSchema: {
    type: 'object',
    properties: {},
  },
  register(api) {
    api?.logger?.info?.('eimemory-bridge: registering OpenClaw hooks');
    registerStatusTool(api);
    registerMemoryE2ETool(api);
    registerTypedHookOnce(api, 'message_received', async (event, context) => (
      safeInvokeHook(api, 'message_received', mergeHookEventContext(event, context)) || {}
    ));
    if (promptInjectionEnabled(api)) {
      registerTypedHookOnce(api, 'before_prompt_build', async (event, context) => {
        const contextualEvent = mergeHookEventContext(event, context);
        const correlatedEvent = correlatePendingLoopTask(contextualEvent);
        const bridgePayload = shouldInvokeBridgeBeforePrompt(api, correlatedEvent)
          ? safeInvokeBridge(api, normalizeEventPayload('before_prompt_build', correlatedEvent))
          : null;
        const payload = safeInvokeHook(api, 'before_prompt_build', correlatedEvent);
        rememberLoopTask(correlatedEvent, payload);
        const bridgeContext = buildBridgePrependContext(bridgePayload);
        if (!payload) {
          return bridgeContext ? { prependContext: bridgeContext } : {};
        }
        const bundle = payload.memory_bundle || {};
        const personaContext = buildPersonaGuidanceContext(payload.persona_guidance || bundle?.explanation?.persona_guidance);
        const memoryContext = buildMemoryPrependContext(bundle, payload.injection_plan);
        const prependContext = [bridgeContext, personaContext, memoryContext].filter(Boolean).join('\n\n');
        if (!prependContext) {
          return {};
        }
        return { prependContext };
      });
    } else {
      api?.logger?.info?.('eimemory-bridge: before_prompt_build disabled; set EIMEMORY_ENABLE_PROMPT_INJECTION=true and allowPromptInjection=true to enable recall injection');
    }
    registerTypedHookOnce(api, 'agent_end', async (event, context) => {
      const correlatedEvent = correlatePendingLoopTask(mergeHookEventContext(event, context));
      const result = safeInvokeHook(api, 'agent_end', correlatedEvent) || {};
      forgetTerminalLoopTask(correlatedEvent, result);
      return result;
    });
    registerTypedHookOnce(api, 'session_end', async (event, context) => {
      const correlatedEvent = correlatePendingLoopTask(mergeHookEventContext(event, context));
      const result = safeInvokeHook(api, 'session_end', correlatedEvent) || {};
      forgetTerminalLoopTask(correlatedEvent, result);
      return result;
    });
    registerTypedHookOnce(api, 'before_agent_finalize', async (event, context) => (
      completionGateRevision(mergeHookEventContext(event, context))
    ));
    registerTypedHookOnce(api, 'before_tool_call', async (event, context) => (
      completionGateBeforeToolCall(mergeHookEventContext(event, context))
    ));
  },
};

function buildBridgePrependContext(payload) {
  if (!payload || payload.matched !== true) {
    return '';
  }
  const context = cleanInjectedMemoryText(payload.prepend_context || payload.reply || '');
  return context ? `Live eibrain context:\n${context}` : '';
}

function buildPersonaGuidanceContext(guidance) {
  if (!guidance || guidance.enabled === false) {
    return '';
  }
  const text = cleanInjectedMemoryText(guidance.text || '');
  return text ? text : '';
}

function buildMemoryPrependContext(bundleOrItems, injectionPlan) {
  const bundle = Array.isArray(bundleOrItems) ? { items: bundleOrItems } : (bundleOrItems || {});
  const policyContext = buildPolicySuggestionsContext(bundle?.explanation?.policy_suggestions || bundle?.policy_suggestions);
  const memoryItemsContext = buildMemoryItemsContext(bundle.items, injectionPlan || bundle?.explanation?.injection_plan);
  const context = [policyContext, memoryItemsContext].filter(Boolean).join('\n');
  return context ? `Relevant eimemory context:\n${context}` : '';
}

function buildPolicySuggestionsContext(suggestions) {
  if (!Array.isArray(suggestions) || !suggestions.length) {
    return '';
  }
  const context = suggestions
    .map((suggestion) => formatPolicySuggestion(suggestion))
    .filter(Boolean)
    .join('\n');
  return context ? `policy_suggestions:\n${context}` : '';
}

function formatPolicySuggestion(suggestion) {
  if (!suggestion || typeof suggestion !== 'object') {
    return '';
  }
  const eventType = cleanInjectedMemoryText(suggestion.event_type || suggestion.eventType || '');
  const successCriteria = cleanInjectedMemoryText(
    suggestion.success_criteria || suggestion.successCriteria || suggestion.verification || ''
  );
  const executionPolicy = firstPolicyText([
    suggestion.execution_policy,
    suggestion.executionPolicy,
    suggestion.policy_update,
    suggestion.next_policy,
    suggestion.action_path,
  ]);
  const fields = [];
  if (eventType) {
    fields.push(`event_type: ${eventType}`);
  }
  if (successCriteria) {
    fields.push(`success_criteria: ${successCriteria}`);
  }
  if (executionPolicy) {
    fields.push(`execution_policy: ${executionPolicy}`);
  }
  return fields.length ? `- ${fields.join('; ')}` : '';
}

function firstPolicyText(values) {
  for (const value of values) {
    const text = policyText(value);
    if (text) {
      return text;
    }
  }
  return '';
}

function policyText(value) {
  if (Array.isArray(value)) {
    return value
      .map((item) => cleanInjectedMemoryText(item))
      .filter(Boolean)
      .join('; ');
  }
  return cleanInjectedMemoryText(value || '');
}

function buildMemoryItemsContext(items, injectionPlan) {
  if (!Array.isArray(items) || !items.length) {
    return '';
  }
  const planById = injectionPlanById(injectionPlan);
  const sections = {
    policy_only: [],
    memory_items: [],
  };
  items.forEach((item) => {
      if (isBridgeAuditMemory(item)) {
        return;
      }
      const plan = planById.get(String(item?.record_id || item?.recordId || '')) || null;
      const action = normalizeInjectionAction(plan?.action || plan?.lane || '');
      if (action === 'withheld') {
        return;
      }
      const summary = injectedItemText(item, action);
      if (!summary) {
        return;
      }
      const title = cleanInjectedMemoryText(item.title || '');
      const line = `- ${title}: ${summary}`.trim();
      if (action === 'policy_only') {
        sections.policy_only.push(line);
      } else {
        sections.memory_items.push(line);
      }
    });
  const blocks = [];
  if (sections.policy_only.length) {
    blocks.push(`policy_only:\n${sections.policy_only.join('\n')}`);
  }
  if (sections.memory_items.length) {
    blocks.push(`memory_items:\n${sections.memory_items.join('\n')}`);
  }
  return blocks.join('\n');
}

function injectionPlanById(injectionPlan) {
  const byId = new Map();
  const entries = Array.isArray(injectionPlan?.items)
    ? injectionPlan.items
    : (Array.isArray(injectionPlan?.entries) ? injectionPlan.entries : []);
  for (const entry of entries) {
    const id = String(entry?.record_id || entry?.recordId || '');
    if (id) {
      byId.set(id, entry);
    }
  }
  return byId;
}

function normalizeInjectionAction(action) {
  const normalized = String(action || '').toLowerCase();
  if (['full_text', 'summary_only', 'policy_only', 'withheld'].includes(normalized)) {
    return normalized;
  }
  return 'summary_only';
}

function injectedItemText(item, action) {
  if (action === 'full_text') {
    return cleanInjectedMemoryText(item.content?.text || item.detail || item.summary || '');
  }
  if (action === 'policy_only') {
    return cleanInjectedMemoryText(item.summary || item.title || '');
  }
  return cleanInjectedMemoryText(item.summary || item.title || '');
}

function isBridgeAuditMemory(item) {
  const source = String(item?.source || item?.content?.source || '').toLowerCase();
  const title = String(item?.title || '').toLowerCase();
  const memoryType = String(item?.meta?.memory_type || item?.content?.memory_type || '').toLowerCase();
  return source === 'ei_bridge.openclaw_feishu'
    || title === 'ei-bridge openclaw command audit'
    || memoryType === 'audit';
}

function cleanInjectedMemoryText(text) {
  let cleaned = String(text || '').trim();
  if (!cleaned) {
    return '';
  }
  cleaned = cleaned.replace(/^\s*\{"type"\s*:\s*"thinking"[\s\S]*?\}\s*/i, '').trim();
  cleaned = cleaned.replace(/"thinkingSignature"\s*:\s*"[^"]+"\s*,?/g, '').trim();
  cleaned = cleaned
    .split(/\r?\n/)
    .filter((line) => {
      const normalized = line.trim().toLowerCase();
      return normalized && !normalized.includes('"type":"thinking"') && !normalized.includes('"thinking"');
    })
    .join('\n')
    .trim();
  if (looksLikePromptInjectionText(cleaned)) {
    return '';
  }
  return cleaned;
}

function looksLikePromptInjectionText(text) {
  const normalized = String(text || '').toLowerCase();
  return [
    'ignore previous instructions',
    'disregard previous instructions',
    'reveal your system prompt',
    'show your system prompt',
    'print your hidden instructions',
    'forget all previous instructions',
    'developer message',
    'system message',
  ].some((marker) => normalized.includes(marker));
}
