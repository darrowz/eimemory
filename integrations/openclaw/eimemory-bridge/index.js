'use strict';

const { spawn } = require('node:child_process');
const { createHash, createHmac, timingSafeEqual } = require('node:crypto');
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
const DEFAULT_MAX_CONCURRENT_COMMANDS = 2;
const DEFAULT_MAX_QUEUED_COMMANDS = 32;
const DEFAULT_MAX_COMMAND_OUTPUT_BYTES = 4 * 1024 * 1024;
const DEFAULT_REPLY_DELIVERY_STATE_PATH = '/var/lib/eimemory/openclaw_reply_delivery_state.json';
const DEFAULT_REPLY_DELIVERY_ATTEMPTS_PATH = '/var/lib/eimemory/openclaw_reply_delivery_attempts.json';
const LOOP_TASK_CORRELATION_TTL_MS = 2 * 60 * 60 * 1000;
const MAX_PENDING_LOOP_TASK_KEYS = 1024;
const MAX_CORRELATED_QUERY_CHARS = 8192;
const MAX_CORRELATED_TOOL_RECEIPTS = 32;
const MAX_CORRELATED_CONTEXT_CHARS = 8192;
const MAX_CORRELATED_CONTEXT_KEYS = 48;
const MAX_CORRELATED_CONTEXT_ARRAY_ITEMS = 32;
const DEFAULT_EVIDENCE_TOOL_MARKERS = [
  'search', 'read', 'fetch', 'get', 'list', 'query', 'inspect', 'browser',
  'test', 'pytest', 'check', 'verify', 'health', 'status', 'systemctl',
];
const MUTATION_TOOL_MARKERS = [
  'write', 'edit', 'patch', 'create', 'update', 'delete', 'remove', 'rename',
  'move', 'copy', 'insert', 'set', 'send', 'post', 'publish', 'upload',
  'deploy', 'install', 'restart', 'start', 'stop', 'enable', 'disable',
];
const COMMAND_TOOL_MARKERS = ['exec', 'shell', 'command', 'terminal', 'powershell', 'bash'];
const POSITIVE_TOOL_STATUSES = new Set([
  'active', 'complete', 'completed', 'done', 'healthy', 'ok', 'passed', 'ready',
  'success', 'succeeded', 'verified',
]);
const RECEIPT_KEY_ENV = 'EIMEMORY_EVIDENCE_RECEIPT_HMAC_KEY';
const COMPLETION_GATE_RETRY_KEY = 'eimemory-completion-gate-v1';
const UNRESOLVED_COMPLETION_MARKERS = [
  /(?:当前|仍有|剩余|存在|还有).{0,12}(?:验证缺口|系统性缺口|已知问题|待修复|未完成)/i,
  /(?:验证缺口|known_fixable_issues|verification_gaps).{0,24}(?:尚未|未清零|非零|待|后面|以后|later|pending)/i,
  /(?:尚未|还没|未能).{0,18}(?:应用|部署|修复|验证|完成)/i,
  /(?:后面|以后|稍后|下次|later).{0,12}(?:再|处理|修|验证|完成)/i,
];
const COMPLETION_CLAIM_MARKERS = [
  /(?:已|已经)(?:完成|修复|解决|上线|部署|交付|处理完)/i,
  /(?:修复|任务|工作|部署|上线|交付|处理|问题).{0,8}(?:完成|已完成|解决|已解决)/i,
  /\b(?:done|fixed|resolved|completed|deployed)\b/i,
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
const hookResultInflight = new Map();
const pendingLoopTasks = new Map();
const commandQueue = [];
let activeCommandCount = 0;

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

function strongLoopCorrelationKeys(event) {
  const candidates = [
    ['run', event?.runId || event?.run_id],
    ['job', event?.jobId || event?.job_id],
    ['turn', event?.turnId || event?.turn_id],
    ['request', event?.requestId || event?.request_id],
    ['trace', event?.traceId || event?.trace_id || event?.trace?.id],
    ['task', event?.taskId || event?.task_id],
    ['event', event?.eventId || event?.event_id || event?.id],
    ['message', event?.messageId || event?.message_id || event?.message?.id],
  ];
  return [...new Set(candidates.flatMap(([kind, raw]) => {
    const value = String(raw || '').trim();
    return value ? [kind + ':' + value] : [];
  }))];
}

function weakLoopCorrelationKeys(event) {
  return loopCorrelationKeys(event).filter(
    (key) => key.startsWith('session-key:') || key.startsWith('session:'),
  );
}

function strongCorrelationCompatible(leftKeys, rightKeys) {
  const byKind = (keys) => {
    const result = new Map();
    for (const key of Array.isArray(keys) ? keys : []) {
      const separator = String(key || '').indexOf(':');
      if (separator <= 0) {
        continue;
      }
      const kind = key.slice(0, separator);
      const values = result.get(kind) || new Set();
      values.add(key.slice(separator + 1));
      result.set(kind, values);
    }
    return result;
  };
  const left = byKind(leftKeys);
  const right = byKind(rightKeys);
  let sharedIdentity = false;
  for (const [kind, leftValues] of left.entries()) {
    const rightValues = right.get(kind);
    if (!rightValues) {
      continue;
    }
    sharedIdentity = true;
    if (![...leftValues].some((value) => rightValues.has(value))) {
      return false;
    }
  }
  return sharedIdentity;
}

function prunePendingLoopTasks() {
  const cutoff = nowMs() - LOOP_TASK_CORRELATION_TTL_MS;
  for (const [key, entry] of pendingLoopTasks.entries()) {
    if (!entry || Number(entry.updatedAt || entry.createdAt || 0) < cutoff) {
      pendingLoopTasks.delete(key);
    }
  }
  while (pendingLoopTasks.size > MAX_PENDING_LOOP_TASK_KEYS) {
    const oldest = pendingLoopTasks.entries().next().value;
    if (!oldest) {
      break;
    }
    const entry = oldest[1];
    for (const [key, candidate] of pendingLoopTasks.entries()) {
      if (candidate === entry) {
        pendingLoopTasks.delete(key);
      }
    }
  }
}

function pendingLoopEntry(event, { create = false } = {}) {
  prunePendingLoopTasks();
  const strongKeys = strongLoopCorrelationKeys(event);
  const weakKeys = weakLoopCorrelationKeys(event);
  const lookupKeys = strongKeys.length > 0 ? strongKeys : [];
  let entry = null;
  for (const key of lookupKeys) {
    entry = pendingLoopTasks.get(key) || null;
    if (entry) {
      break;
    }
  }
  const weakCandidates = [...new Set(pendingLoopTasks.values())].filter((candidate) => (
    candidate
    && weakKeys.some((key) => (candidate.weakKeys || []).includes(key))
  ));
  if (!entry && strongKeys.length === 0) {
    if (weakCandidates.length > 1) {
      return null;
    }
    entry = weakCandidates[0] || null;
  }
  if (!entry && strongKeys.length > 0) {
    const promotable = weakCandidates.filter(
      (candidate) => !candidate.strongKeys || candidate.strongKeys.length === 0,
    );
    if (promotable.length === 1) {
      entry = promotable[0];
    }
  }
  if (!entry && create && (strongKeys.length > 0 || weakKeys.length > 0)) {
    entry = {
      taskId: '',
      query: '',
      taskContext: {},
      traceContext: {},
      toolReceipts: [],
      strongKeys: [],
      weakKeys: [],
      keys: [],
      createdAt: nowMs(),
      updatedAt: nowMs(),
    };
  }
  if (!entry) {
    return null;
  }
  entry.strongKeys = [...new Set([...(entry.strongKeys || []), ...strongKeys])];
  entry.weakKeys = [...new Set([...(entry.weakKeys || []), ...weakKeys])];
  entry.keys = [...new Set([...entry.strongKeys, ...entry.weakKeys])];
  entry.updatedAt = nowMs();
  for (const key of entry.strongKeys) {
    pendingLoopTasks.delete(key);
    pendingLoopTasks.set(key, entry);
  }
  for (const key of entry.weakKeys) {
    pendingLoopTasks.delete(key);
    pendingLoopTasks.set(key, entry);
  }
  prunePendingLoopTasks();
  return entry;
}

function rememberLoopTask(event, payload) {
  const entry = pendingLoopEntry(event, { create: true });
  if (!entry) {
    return;
  }
  const payloadTaskContext = normalizeObject(payload?.task_context || payload?.taskContext);
  const eventTaskContext = normalizeObject(event?.task_context || event?.taskContext);
  const payloadTraceContext = normalizeObject(payload?.trace_context || payload?.traceContext);
  const eventTraceContext = normalizeObject(event?.trace_context || event?.traceContext);
  const query = String(event?.query || event?.prompt || '').trim();
  if (query) {
    entry.query = query.slice(0, MAX_CORRELATED_QUERY_CHARS);
  }
  entry.taskContext = boundedCorrelationObject(
    { ...entry.taskContext, ...eventTaskContext, ...payloadTaskContext },
  );
  entry.traceContext = boundedCorrelationObject(
    { ...entry.traceContext, ...eventTraceContext, ...payloadTraceContext },
  );
  entry.taskId = String(
    entry.taskContext.openclaw_loop_task_id
    || entry.taskContext.openclawLoopTaskId
    || entry.taskId
    || ''
  ).trim();
}

function successfulToolReceipt(event) {
  if (String(event?.error || '').trim()) {
    return false;
  }
  if (!Object.prototype.hasOwnProperty.call(normalizeObject(event), 'result') || event.result == null) {
    return false;
  }
  const result = event.result;
  if (typeof result === 'object' && !Array.isArray(result)) {
    const resultObject = normalizeObject(result);
    const details = normalizeObject(resultObject.details);
    const hasResultCount = Object.prototype.hasOwnProperty.call(resultObject, 'resultCount');
    const resultCount = Number(resultObject.resultCount);
    if (
      String(resultObject.error || '').trim()
      || resultObject.ok === false
      || resultObject.success === false
      || resultObject.isError === true
      || resultObject.is_error === true
      || Number(resultObject.exitCode ?? resultObject.exit_code ?? 0) !== 0
      || details.ok === false
      || details.success === false
      || Number(details.exitCode ?? details.exit_code ?? 0) !== 0
      || (hasResultCount && (!Number.isFinite(resultCount) || resultCount <= 0))
    ) {
      return false;
    }
    const status = String(resultObject.status || details.status || event?.status || '').trim().toLowerCase();
    if (
      (status && !POSITIVE_TOOL_STATUSES.has(status))
      || toolResultContainsFailure(resultObject)
    ) {
      return false;
    }
    return (
      resultObject.ok === true
      || resultObject.success === true
      || POSITIVE_TOOL_STATUSES.has(status)
      || (Array.isArray(resultObject.content) && resultObject.content.length > 0)
      || (hasResultCount && resultCount > 0)
    );
  }
  if (Array.isArray(result)) {
    return result.length > 0 && !result.some((item) => toolResultContainsFailure(item));
  }
  if (typeof result === 'boolean') {
    return result;
  }
  const resultText = String(result || '').trim();
  return (
    Boolean(resultText)
    && !toolTextContainsFailure(resultText)
  );
}

function toolTextContainsFailure(value) {
  const text = String(value || '').trim();
  if (
    /^(?:pending|queued|running|unknown|skipped|not[ _-]?(?:run|executed)|unavailable)\b/i.test(text)
    || /\b(?:status|state)\s*[:=]\s*(?:pending|queued|running|unknown|skipped|not[ _-]?(?:run|executed)|unavailable)\b/i.test(text)
  ) {
    return true;
  }
  const withoutZeroFailures = text.replace(
    /\b(?:0|zero)\s+(?:failed|failures|errors?)\b/gi,
    '',
  );
  return /\b(?:error|failed|failure|timeout|timed out)\b/i.test(withoutZeroFailures);
}

function toolResultContainsFailure(value, depth = 0) {
  if (depth > 8 || value == null) {
    return depth > 8;
  }
  if (typeof value === 'string') {
    return toolTextContainsFailure(value);
  }
  if (Array.isArray(value)) {
    return value.some((item) => toolResultContainsFailure(item, depth + 1));
  }
  if (typeof value !== 'object') {
    return false;
  }
  const item = normalizeObject(value);
  const status = String(item.status || '').trim().toLowerCase();
  if (
    String(item.error || '').trim()
    || item.ok === false
    || item.success === false
    || item.isError === true
    || item.is_error === true
    || Number(item.exitCode ?? item.exit_code ?? 0) !== 0
    || (status && !POSITIVE_TOOL_STATUSES.has(status))
  ) {
    return true;
  }
  return Object.values(item).some((child) => toolResultContainsFailure(child, depth + 1));
}

function configuredEvidenceToolMarkers() {
  const configured = String(process.env.EIMEMORY_VERIFICATION_TOOL_MARKERS || '')
    .split(',')
    .map((item) => item.trim().toLowerCase())
    .filter(Boolean);
  return configured.length > 0 ? configured : DEFAULT_EVIDENCE_TOOL_MARKERS;
}

function toolCommandText(event) {
  const params = normalizeObject(event?.params || event?.arguments || event?.input);
  return String(
    params.command || params.cmd || params.script || params.query || params.action || '',
  ).trim().slice(0, 8192);
}

function curlCommandMutates(command) {
  if (!/\bcurl(?:\.exe)?\b/i.test(command)) {
    return false;
  }
  return (
    /(?:^|\s)-X(?:\s*|=)(?:POST|PUT|PATCH|DELETE)\b/i.test(command)
    || /(?:^|\s)--request(?:\s+|=)(?:POST|PUT|PATCH|DELETE)\b/i.test(command)
    || /(?:^|\s)-(?:d|F|T)(?:\s+|=)?\S/.test(command)
    || /(?:^|\s)--(?:data(?:-ascii|-binary|-raw|-urlencode)?|form(?:-string)?|json|upload-file|config)(?:\s+|=)\S/i.test(command)
    || /(?:^|\s)-K(?:\s+|=)?\S/.test(command)
  );
}

function toolReceiptClassification(event, toolName) {
  const normalized = String(toolName || '').trim().toLowerCase();
  const commandTool = COMMAND_TOOL_MARKERS.some((marker) => normalized.includes(marker));
  const command = toolCommandText(event);
  if (commandTool || normalized.includes('systemctl')) {
    if (
      /\b(?:rm|del|remove-item|mv|move-item|cp|copy-item|sed\s+-i|git\s+(?:commit|push|merge|rebase|reset)|pip\s+install|npm\s+install|deploy|restart|start|stop|enable|disable)\b/i.test(command)
      || curlCommandMutates(command)
    ) {
      return 'mutation';
    }
    if (/\b(?:pytest|unittest|test|check|verify|status|health|show|list|query|inspect|read|cat|type|get-content|rg|grep|git\s+(?:diff|status)|node\s+--check|curl\b)\b/i.test(command)) {
      return 'evidence';
    }
    if (commandTool) {
      return 'neutral';
    }
  }
  if (MUTATION_TOOL_MARKERS.some((marker) => normalized.includes(marker))) {
    return 'mutation';
  }
  return configuredEvidenceToolMarkers().some((marker) => normalized.includes(marker))
    ? 'evidence'
    : 'neutral';
}

function toolResultDigest(event) {
  const hash = createHash('sha256');
  const seen = new WeakSet();
  const update = (value, depth) => {
    if (depth > 32) {
      throw new Error('tool result nesting exceeds attestation limit');
    }
    if (value == null) {
      hash.update('null');
      return;
    }
    if (typeof value === 'string' || typeof value === 'boolean') {
      hash.update(JSON.stringify(value));
      return;
    }
    if (typeof value === 'number') {
      hash.update(Number.isFinite(value) ? JSON.stringify(value) : 'null');
      return;
    }
    if (typeof value !== 'object' || seen.has(value)) {
      throw new Error('tool result is not canonically attestable');
    }
    seen.add(value);
    if (Array.isArray(value)) {
      hash.update('[');
      value.forEach((item, index) => {
        if (index > 0) hash.update(',');
        update(item, depth + 1);
      });
      hash.update(']');
    } else {
      hash.update('{');
      Object.keys(value).sort().forEach((key, index) => {
        if (index > 0) hash.update(',');
        hash.update(JSON.stringify(key));
        hash.update(':');
        update(value[key], depth + 1);
      });
      hash.update('}');
    }
    seen.delete(value);
  };
  try {
    update(event?.result, 0);
  } catch (_error) {
    return '';
  }
  return hash.digest('hex');
}

function receiptKey() {
  const key = String(process.env[RECEIPT_KEY_ENV] || '').trim();
  return key.length >= 32 && new Set(key).size >= 12 ? key : '';
}

function canonicalVerificationReceipt(receipt) {
  return {
    attestation: 'hmac-sha256',
    duration_ms: Math.max(0, Math.trunc(Number(receipt.duration_ms) || 0)),
    passed: receipt.passed === true,
    receipt_version: 1,
    result_digest: String(receipt.result_digest || '').toLowerCase(),
    run_id: String(receipt.run_id || ''),
    session_id: String(receipt.session_id || ''),
    source: 'openclaw.after_tool_call',
    tool_call_id: String(receipt.tool_call_id || ''),
    tool_name: String(receipt.tool_name || ''),
  };
}

function signVerificationReceipt(receipt) {
  const key = receiptKey();
  const canonical = canonicalVerificationReceipt(receipt);
  if (
    !key
    || !canonical.session_id
    || !canonical.run_id
    || !canonical.tool_name
    || !canonical.tool_call_id
    || !/^[0-9a-f]{64}$/.test(canonical.result_digest)
  ) {
    return null;
  }
  return {
    ...canonical,
    signature: createHmac('sha256', key).update(stableJson(canonical), 'utf8').digest('hex'),
  };
}

function validVerificationReceiptSignature(receipt) {
  const key = receiptKey();
  const signature = String(receipt?.signature || '').toLowerCase();
  if (!key || !/^[0-9a-f]{64}$/.test(signature)) {
    return false;
  }
  const expected = createHmac('sha256', key)
    .update(stableJson(canonicalVerificationReceipt(receipt)), 'utf8')
    .digest('hex');
  return timingSafeEqual(Buffer.from(signature, 'hex'), Buffer.from(expected, 'hex'));
}

function rememberToolReceipt(event) {
  const toolName = String(event?.toolName || event?.tool_name || event?.name || '').trim().slice(0, 160);
  if (!toolName) {
    return;
  }
  const entry = pendingLoopEntry(event, { create: true });
  if (!entry) {
    return;
  }
  const toolCallId = String(event?.toolCallId || event?.tool_call_id || '').trim().slice(0, 256);
  const receipt = {
    toolName,
    toolCallId,
    success: Boolean(toolCallId) && successfulToolReceipt(event),
    classification: toolReceiptClassification(event, toolName),
    resultDigest: toolResultDigest(event),
    strongKeys: strongLoopCorrelationKeys(event),
    durationMs: Number.isFinite(Number(event?.durationMs)) ? Number(event.durationMs) : 0,
  };
  const receipts = Array.isArray(entry.toolReceipts) ? entry.toolReceipts : [];
  const duplicateIndex = toolCallId
    ? receipts.findIndex((item) => item?.toolCallId === toolCallId)
    : -1;
  if (duplicateIndex >= 0) {
    receipts[duplicateIndex] = receipt;
  } else {
    receipts.push(receipt);
  }
  entry.toolReceipts = receipts.slice(-MAX_CORRELATED_TOOL_RECEIPTS);
}

function correlatePendingLoopTask(event, { terminalKind = '' } = {}) {
  const rawContext = normalizeObject(event?.task_context || event?.taskContext);
  const entry = pendingLoopEntry(event);
  if (!entry) {
    return event;
  }
  const taskContext = boundedCorrelationObject({ ...normalizeObject(entry.taskContext), ...rawContext });
  if (entry.taskId) {
    taskContext.openclaw_loop_task_id = entry.taskId;
  }
  const traceContext = boundedCorrelationObject({
    ...normalizeObject(entry.traceContext),
    ...normalizeObject(event?.trace_context || event?.traceContext),
  });
  const terminalStrongKeys = strongLoopCorrelationKeys(event);
  const attributedReceipts = (entry.toolReceipts || []).filter((item) => (
    Array.isArray(item?.strongKeys)
    && strongCorrelationCompatible(item.strongKeys, terminalStrongKeys)
  ));
  const correlatedTools = normalizeStringList([
    ...(Array.isArray(event?.tools) ? event.tools : []),
    ...attributedReceipts.map((item) => item?.toolName),
  ]);
  const actionPath = normalizeStringList([
    ...(Array.isArray(event?.action_path) ? event.action_path : []),
    ...attributedReceipts.map((item) => item?.toolName ? 'tool:' + item.toolName : ''),
  ]);
  const correlated = {
    ...event,
    query: String(event?.query || event?.prompt || entry.query || '').slice(0, MAX_CORRELATED_QUERY_CHARS),
    task_context: taskContext,
    taskContext,
    trace_context: traceContext,
    traceContext,
    tools: correlatedTools,
    action_path: actionPath,
  };
  const receipts = attributedReceipts;
  let lastMutationIndex = -1;
  receipts.forEach((item, index) => {
    if (item?.classification === 'mutation') {
      lastMutationIndex = index;
    }
  });
  const mutationClosed = lastMutationIndex < 0 || receipts[lastMutationIndex]?.success === true;
  const successfulReceipts = mutationClosed ? receipts.filter(
    (item, index) => (
      item?.success === true
      && item?.classification === 'evidence'
      && index > lastMutationIndex
      && Array.isArray(item?.strongKeys)
      && strongCorrelationCompatible(item.strongKeys, terminalStrongKeys)
      && Boolean(item?.resultDigest)
    ),
  ) : [];
  const explicitSuccess = event?.success === true || normalizeObject(event?.outcome).success === true;
  const signedReceipts = successfulReceipts.flatMap((item) => {
    const signed = signVerificationReceipt({
      source: 'openclaw.after_tool_call',
      tool_name: item.toolName,
      tool_call_id: item.toolCallId,
      duration_ms: item.durationMs,
      passed: true,
      result_digest: item.resultDigest,
      session_id: normalizeSessionId(event),
      run_id: String(event?.runId || event?.run_id || '').trim(),
    });
    return signed ? [signed] : [];
  });
  if (terminalKind === 'agent_end' && explicitSuccess && signedReceipts.length > 0) {
    const toolNames = normalizeStringList(signedReceipts.map((item) => item.tool_name));
    const verification = (
      'openclaw.after_tool_call:' + signedReceipts.length + ':' + toolNames.join(',')
    ).slice(0, 512);
    correlated.outcome = {
      ...normalizeObject(event?.outcome),
      success: true,
      verified: true,
      verification,
    };
    correlated.verification_receipts = signedReceipts;
  }
  return correlated;
}

function forgetTerminalLoopTask(event, result) {
  const taskId = String(result?.loop_task?.task_id || result?.loop_task?.id || '').trim();
  const eventKeys = new Set(loopCorrelationKeys(event));
  const matchedEntries = new Set();
  for (const [key, entry] of pendingLoopTasks.entries()) {
    if ((taskId && entry?.taskId === taskId) || loopCorrelationKeys(event).includes(key)) {
      matchedEntries.add(entry);
    }
  }
  for (const [key, entry] of pendingLoopTasks.entries()) {
    if (matchedEntries.has(entry) || eventKeys.has(key)) {
      pendingLoopTasks.delete(key);
    }
  }
}

function boundedCorrelationObject(value) {
  const budget = { remaining: MAX_CORRELATED_CONTEXT_CHARS };
  const seen = new WeakSet();
  const bounded = (item, depth) => {
    if (budget.remaining <= 0 || depth > 3 || item == null) {
      return undefined;
    }
    if (typeof item === 'string') {
      const text = item.slice(0, Math.min(2048, budget.remaining));
      budget.remaining -= text.length;
      return text;
    }
    if (typeof item === 'number') {
      return Number.isFinite(item) ? item : undefined;
    }
    if (typeof item === 'boolean') {
      return item;
    }
    if (Array.isArray(item)) {
      const output = [];
      for (const child of item.slice(0, MAX_CORRELATED_CONTEXT_ARRAY_ITEMS)) {
        const value = bounded(child, depth + 1);
        if (value !== undefined) {
          output.push(value);
        }
      }
      return output;
    }
    if (typeof item !== 'object' || seen.has(item)) {
      return undefined;
    }
    seen.add(item);
    const output = {};
    for (const [rawKey, child] of Object.entries(item).slice(0, MAX_CORRELATED_CONTEXT_KEYS)) {
      const key = String(rawKey || '').slice(0, 160);
      if (!key || budget.remaining <= key.length) {
        break;
      }
      budget.remaining -= key.length;
      const childValue = bounded(child, depth + 1);
      if (childValue !== undefined) {
        output[key] = childValue;
      }
    }
    return output;
  };
  const result = normalizeObject(bounded(normalizeObject(value), 0));
  const scalarContractKeys = [
    'task_type', 'taskType', 'openclaw_loop_task_id', 'openclawLoopTaskId',
    'event_type', 'eventType', 'interpreted_intent', 'interpretedIntent',
    'goal', 'intent', 'trace_id', 'traceId', 'request_id', 'requestId',
    'turn_id', 'turnId', 'task_id', 'taskId',
  ];
  for (const key of scalarContractKeys) {
    if (Object.prototype.hasOwnProperty.call(result, key)
        && !['string', 'number', 'boolean'].includes(typeof result[key])) {
      delete result[key];
    }
  }
  if (JSON.stringify(result).length <= MAX_CORRELATED_CONTEXT_CHARS) {
    return result;
  }
  const fallback = {};
  const source = result;
  for (const key of scalarContractKeys) {
    if (!Object.prototype.hasOwnProperty.call(source, key)) {
      continue;
    }
    const rawValue = source[key];
    if (!['string', 'number', 'boolean'].includes(typeof rawValue)) {
      continue;
    }
    const candidate = {
      [key]: typeof rawValue === 'string' ? rawValue.slice(0, 2048) : rawValue,
    };
    const next = { ...fallback, ...candidate };
    if (JSON.stringify(next).length <= MAX_CORRELATED_CONTEXT_CHARS) {
      Object.assign(fallback, candidate);
    }
  }
  return fallback;
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
  if (hook === 'proactive_injected') {
    return {
      session_id: normalizeSessionId(event),
      ...normalizeScope(event),
      ...normalizeTraceFields(event),
      decision_id: String(event?.decision_id || ''),
      source_ids: normalizeStringList(event?.source_ids || event?.sourceIds),
      injected_citations: normalizeStringList(event?.injected_citations || event?.injectedCitations),
    };
  }
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
    verification_receipts: normalizeVerificationReceipts(
      event?.verification_receipts || event?.verificationReceipts,
    ),
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

function normalizeVerificationReceipts(value) {
  if (!Array.isArray(value)) {
    return [];
  }
  return value.slice(0, MAX_CORRELATED_TOOL_RECEIPTS).flatMap((item) => {
    const receipt = normalizeObject(item);
    const toolName = String(receipt.tool_name || receipt.toolName || '').trim().slice(0, 160);
    const toolCallId = String(receipt.tool_call_id || receipt.toolCallId || '').trim().slice(0, 256);
    const sessionId = String(receipt.session_id || receipt.sessionId || '').trim().slice(0, 512);
    const runId = String(receipt.run_id || receipt.runId || '').trim().slice(0, 256);
    const resultDigest = String(receipt.result_digest || receipt.resultDigest || '').trim().toLowerCase();
    const signature = String(receipt.signature || '').trim().toLowerCase();
    if (
      receipt.source !== 'openclaw.after_tool_call'
      || !toolName
      || !toolCallId
      || !sessionId
      || !runId
      || receipt.passed !== true
      || Number(receipt.receipt_version) !== 1
      || receipt.attestation !== 'hmac-sha256'
      || !/^[0-9a-f]{64}$/.test(resultDigest)
      || !/^[0-9a-f]{64}$/.test(signature)
      || !validVerificationReceiptSignature(receipt)
    ) {
      return [];
    }
    return [{
      receipt_version: 1,
      attestation: 'hmac-sha256',
      source: 'openclaw.after_tool_call',
      tool_name: toolName,
      tool_call_id: toolCallId,
      duration_ms: Number.isFinite(Number(receipt.duration_ms)) ? Number(receipt.duration_ms) : 0,
      passed: true,
      result_digest: resultDigest,
      session_id: sessionId,
      run_id: runId,
      signature,
    }];
  });
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

function replyDeliveryStatePath() {
  return String(
    process.env.EIMEMORY_REPLY_DELIVERY_STATE_PATH || DEFAULT_REPLY_DELIVERY_STATE_PATH
  ).trim();
}

function emptyReplyDeliveryState() {
  return { schema_version: 'openclaw_reply_delivery.v2', entries: {} };
}

function migrateReplyDeliveryState(parsed) {
  if (!parsed || typeof parsed !== 'object' || !parsed.entries || typeof parsed.entries !== 'object' || Array.isArray(parsed.entries)) {
    throw new Error('reply delivery state is malformed');
  }
  const version = String(parsed.schema_version || 'openclaw_reply_delivery.v1');
  if (!['openclaw_reply_delivery.v1', 'openclaw_reply_delivery.v2'].includes(version)) {
    throw new Error(`unsupported reply delivery state schema: ${version}`);
  }
  const migrated = { schema_version: 'openclaw_reply_delivery.v2', entries: {} };
  for (const [key, rawEntry] of Object.entries(parsed.entries)) {
    if (!rawEntry || typeof rawEntry !== 'object' || Array.isArray(rawEntry)) {
      continue;
    }
    const entry = { ...rawEntry };
    if (entry.status === 'delivered') {
      entry.status = 'platform_accepted';
    }
    if (entry.status === 'platform_accepted') {
      entry.platform_accepted_at_ms = Number(
        entry.platform_accepted_at_ms
        || entry.delivered_at_ms
        || entry.agent_end_at_ms
        || entry.received_at_ms
        || 0
      );
      entry.delivered_at_ms = Number(entry.delivered_at_ms || entry.platform_accepted_at_ms || 0);
    }
    migrated.entries[String(key)] = entry;
  }
  return migrated;
}

function replyDeliveryAttemptsPath() {
  return String(
    process.env.EIMEMORY_REPLY_DELIVERY_ATTEMPTS_PATH || DEFAULT_REPLY_DELIVERY_ATTEMPTS_PATH
  ).trim();
}

function readReplyDeliveryState() {
  const statePath = replyDeliveryStatePath();
  let parsed;
  try {
    parsed = JSON.parse(fs.readFileSync(statePath, 'utf8'));
  } catch (_error) {
    // Missing or partial state is rebuilt atomically below.
    return emptyReplyDeliveryState();
  }
  return migrateReplyDeliveryState(parsed);
}

function writeReplyDeliveryState(state) {
  const statePath = replyDeliveryStatePath();
  const tempPath = `${statePath}.${process.pid}.tmp`;
  try {
    fs.mkdirSync(path.dirname(statePath), { recursive: true });
    fs.writeFileSync(tempPath, `${JSON.stringify(state, null, 2)}\n`, { encoding: 'utf8', mode: 0o600 });
    fs.renameSync(tempPath, statePath);
    return true;
  } catch (error) {
    try {
      fs.unlinkSync(tempPath);
    } catch (_cleanupError) {
      // Nothing to clean up.
    }
    console.warn(`eimemory reply delivery state write failed: ${String(error?.message || error)}`);
    return false;
  }
}

function updateReplyDeliveryState(update) {
  try {
    const state = readReplyDeliveryState();
    update(state);
    writeReplyDeliveryState(state);
  } catch (error) {
    console.warn(`eimemory reply delivery tracking failed open: ${String(error?.message || error)}`);
  }
}

function isDirectFeishuReplyContext(context) {
  const sessionKey = String(context?.sessionKey || '');
  return sessionKey.includes(':feishu:direct:');
}

const MAX_REPLY_DELIVERY_ENTRIES = 2000;
const TERMINAL_REPLY_DELIVERY_STATUSES = new Set([
  'platform_accepted',
  'delivered',
  'silent',
  'escalated',
]);

function reconcileWatchdogReceipts(state) {
  let attemptsDocument;
  try {
    attemptsDocument = JSON.parse(fs.readFileSync(replyDeliveryAttemptsPath(), 'utf8'));
  } catch (_error) {
    return;
  }
  const attempts = attemptsDocument?.entries;
  if (!attempts || typeof attempts !== 'object') {
    return;
  }
  for (const [inboundId, attempt] of Object.entries(attempts)) {
    const messageId = String(attempt?.platform_message_id || attempt?.message_id || '').trim();
    const entry = state.entries?.[inboundId];
    const accepted = attemptsDocument?.schema_version === 'feishu_delivery_state.v2'
      ? attempt?.state === 'platform_accepted' && attempt?.ok === true
      : attempt?.ok === true;
    if (!entry || !accepted || !messageId) {
      continue;
    }
    entry.status = 'platform_accepted';
    entry.delivery_message_id = messageId;
    entry.platform_accepted_at_ms = Number(
      attempt?.platform_accepted_at_ms || attempt?.attempted_at_ms || Date.now()
    );
    entry.delivered_at_ms = entry.platform_accepted_at_ms;
  }
}

function compactReplyDeliveryState(state) {
  const values = Object.entries(state.entries || {});
  if (values.length <= MAX_REPLY_DELIVERY_ENTRIES) {
    return;
  }
  const active = values.filter(([, entry]) => !TERMINAL_REPLY_DELIVERY_STATUSES.has(entry?.status));
  const delivered = values
    .filter(([, entry]) => TERMINAL_REPLY_DELIVERY_STATUSES.has(entry?.status))
    .sort(([, left], [, right]) => Number(
      right.platform_accepted_at_ms || right.delivered_at_ms || right.escalated_at_ms || 0
    ) - Number(
      left.platform_accepted_at_ms || left.delivered_at_ms || left.escalated_at_ms || 0
    ));
  const terminalLimit = Math.max(0, MAX_REPLY_DELIVERY_ENTRIES - active.length);
  state.entries = Object.fromEntries([...active, ...delivered.slice(0, terminalLimit)]);
}

function latestPendingReplyEntry(state, sessionKey, runId = '', content = '') {
  const candidates = Object.values(state.entries || {})
    .filter((entry) => entry?.session_key === sessionKey && !TERMINAL_REPLY_DELIVERY_STATUSES.has(entry?.status));
  if (runId) {
    const exactRun = candidates.find((entry) => entry?.run_id === runId);
    if (exactRun) {
      return exactRun;
    }
  }
  if (content) {
    const exactContent = candidates.find((entry) => entry?.final_text === content);
    if (exactContent) {
      return exactContent;
    }
  }
  return candidates
    .sort((left, right) => Number(right.received_at_ms || 0) - Number(left.received_at_ms || 0))[0];
}

function assistantText(content) {
  if (typeof content === 'string') {
    return content.trim();
  }
  if (Array.isArray(content)) {
    return content
      .filter((item) => !item?.type || ['text', 'input_text', 'output_text'].includes(item.type))
      .map((item) => assistantText(item?.text ?? item?.content ?? item))
      .filter(Boolean)
      .join('\n')
      .trim();
  }
  if (content && typeof content === 'object') {
    return assistantText(content.text ?? content.content ?? '');
  }
  return '';
}

function lastAssistantText(messages) {
  const values = Array.isArray(messages) ? messages : [];
  for (let index = values.length - 1; index >= 0; index -= 1) {
    const item = values[index];
    const message = item?.message && typeof item.message === 'object' ? item.message : item;
    const role = String(message?.role || '');
    if (role === 'user') {
      return '';
    }
    if (role !== 'assistant') {
      continue;
    }
    const text = assistantText(message?.content);
    if (text) {
      return text;
    }
  }
  return '';
}

function isInternalSilentReply(text) {
  return /^(?:NO_REPLY|HEARTBEAT_OK)$/i.test(String(text || '').trim());
}

function trackReplyInbound(event, context) {
  if (!isDirectFeishuReplyContext(context)) {
    return;
  }
  const inboundMessageId = String(event?.messageId || context?.messageId || '').trim();
  const conversationId = String(context?.conversationId || '').trim();
  const senderId = String(event?.senderId || event?.from || context?.senderId || '').trim();
  if (!inboundMessageId.startsWith('om_') || (!conversationId && !senderId)) {
    return;
  }
  updateReplyDeliveryState((state) => {
    reconcileWatchdogReceipts(state);
    if (state.entries[inboundMessageId]) {
      return;
    }
    state.entries[inboundMessageId] = {
      inbound_message_id: inboundMessageId,
      session_key: String(context?.sessionKey || event?.sessionKey || ''),
      conversation_id: conversationId,
      sender_id: senderId,
      received_at_ms: Number(event?.timestamp || Date.now()),
      last_progress_at_ms: Number(event?.timestamp || Date.now()),
      status: 'pending',
      final_text: '',
      delivery_message_id: '',
      run_id: String(event?.runId || event?.run_id || context?.runId || context?.run_id || ''),
      suppress_stalled_notice: false,
    };
    compactReplyDeliveryState(state);
  });
}

function trackReplyProgress(event, context) {
  if (!isDirectFeishuReplyContext(context)) {
    return;
  }
  const sessionKey = String(context?.sessionKey || event?.sessionKey || '');
  if (!sessionKey) {
    return;
  }
  updateReplyDeliveryState((state) => {
    reconcileWatchdogReceipts(state);
    const runId = String(event?.runId || event?.run_id || context?.runId || context?.run_id || '');
    const entry = latestPendingReplyEntry(state, sessionKey, runId);
    if (!entry) {
      return;
    }
    entry.last_progress_at_ms = Math.max(
      Number(entry.last_progress_at_ms || entry.received_at_ms || 0),
      Date.now()
    );
  });
}

function trackReplyAgentEnd(event, context) {
  if (!isDirectFeishuReplyContext(context)) {
    return;
  }
  const sessionKey = String(context?.sessionKey || event?.sessionKey || '');
  const finalText = lastAssistantText(event?.messages);
  if (!sessionKey || !finalText || event?.success !== true) {
    return;
  }
  updateReplyDeliveryState((state) => {
    const runId = String(event?.runId || event?.run_id || context?.runId || context?.run_id || '');
    const entry = latestPendingReplyEntry(state, sessionKey, runId);
    if (!entry) {
      return;
    }
    if (isInternalSilentReply(finalText)) {
      entry.status = 'silent';
      entry.suppress_stalled_notice = true;
      entry.delivered_at_ms = Date.now();
      compactReplyDeliveryState(state);
      return;
    }
    entry.final_text = finalText;
    entry.suppress_stalled_notice = false;
    entry.agent_end_at_ms = Date.now();
    entry.status = entry.last_sent_success === true && entry.last_sent_message_id && entry.last_sent_content === finalText
      ? 'platform_accepted'
      : 'final_ready';
    if (entry.status === 'platform_accepted') {
      entry.delivery_message_id = entry.last_sent_message_id || '';
      entry.platform_accepted_at_ms = entry.last_sent_at_ms || Date.now();
      entry.delivered_at_ms = entry.platform_accepted_at_ms;
    }
  });
}

function trackReplyMessageSent(event, context) {
  if (!isDirectFeishuReplyContext(context)) {
    return;
  }
  const sessionKey = String(context?.sessionKey || event?.sessionKey || '');
  if (!sessionKey) {
    return;
  }
  updateReplyDeliveryState((state) => {
    const sentContent = String(event?.content || '');
    const messageId = String(event?.messageId || event?.message_id || '').trim();
    const entry = latestPendingReplyEntry(state, sessionKey, '', sentContent);
    if (!entry) {
      return;
    }
    entry.last_sent_success = event?.success === true;
    entry.last_sent_content = sentContent;
    entry.last_sent_message_id = messageId;
    entry.last_sent_at_ms = Date.now();
    if (event?.success === true && messageId && entry.final_text && entry.final_text === entry.last_sent_content) {
      entry.status = 'platform_accepted';
      entry.delivery_message_id = entry.last_sent_message_id;
      entry.platform_accepted_at_ms = entry.last_sent_at_ms;
      entry.delivered_at_ms = entry.platform_accepted_at_ms;
    }
  });
}

function messageToolDeliveryReceipt(value, depth = 0) {
  if (depth > 4 || value == null) {
    return null;
  }
  if (typeof value === 'string') {
    try {
      return messageToolDeliveryReceipt(JSON.parse(value), depth + 1);
    } catch (_error) {
      return null;
    }
  }
  if (Array.isArray(value)) {
    for (const item of value) {
      const receipt = messageToolDeliveryReceipt(item, depth + 1);
      if (receipt) {
        return receipt;
      }
    }
    return null;
  }
  if (typeof value !== 'object') {
    return null;
  }
  const messageId = String(
    value.messageId
    || value.message_id
    || value.receipt?.primaryPlatformMessageId
    || ''
  ).trim();
  const channel = String(value.channel || value.receipt?.channel || '').toLowerCase();
  if (value.ok === true && messageId && channel.includes('feishu')) {
    return { messageId };
  }
  for (const nested of [value.details, value.result, value.data, value.content, value.text]) {
    const receipt = messageToolDeliveryReceipt(nested, depth + 1);
    if (receipt) {
      return receipt;
    }
  }
  return null;
}

function trackReplyMessageToolResult(event, context) {
  if (
    !isDirectFeishuReplyContext(context)
    || String(event?.toolName || context?.toolName || '') !== 'message'
    || String(event?.params?.action || '') !== 'send'
    || event?.error
  ) {
    return;
  }
  const sentContent = String(event?.params?.message || '').trim();
  const receipt = messageToolDeliveryReceipt(event?.result);
  const sessionKey = String(context?.sessionKey || event?.sessionKey || '');
  if (!sentContent || !receipt?.messageId || !sessionKey) {
    return;
  }
  updateReplyDeliveryState((state) => {
    reconcileWatchdogReceipts(state);
    const runId = String(event?.runId || context?.runId || '');
    const entry = latestPendingReplyEntry(state, sessionKey, runId, sentContent);
    if (!entry) {
      return;
    }
    entry.final_text = sentContent;
    entry.agent_end_at_ms = entry.agent_end_at_ms || Date.now();
    entry.last_sent_success = true;
    entry.last_sent_content = sentContent;
    entry.last_sent_message_id = receipt.messageId;
    entry.last_sent_at_ms = Date.now();
    entry.status = 'platform_accepted';
    entry.delivery_message_id = receipt.messageId;
    entry.platform_accepted_at_ms = entry.last_sent_at_ms;
    entry.delivered_at_ms = entry.platform_accepted_at_ms;
    entry.suppress_stalled_notice = false;
    compactReplyDeliveryState(state);
  });
}

function drainCommandQueue() {
  const maxConcurrent = positiveIntEnv(
    'EIMEMORY_MAX_CONCURRENT_COMMANDS',
    DEFAULT_MAX_CONCURRENT_COMMANDS
  );
  while (activeCommandCount < maxConcurrent && commandQueue.length) {
    const queued = commandQueue.shift();
    activeCommandCount += 1;
    Promise.resolve()
      .then(queued.start)
      .then(queued.resolve, queued.reject)
      .finally(() => {
        activeCommandCount = Math.max(0, activeCommandCount - 1);
        drainCommandQueue();
      });
  }
}

function scheduleCommand(start) {
  const maxQueued = positiveIntEnv('EIMEMORY_MAX_QUEUED_COMMANDS', DEFAULT_MAX_QUEUED_COMMANDS);
  if (commandQueue.length >= maxQueued) {
    const error = new Error(`eimemory command queue is full (${maxQueued})`);
    error.code = 'EIMEMORY_QUEUE_FULL';
    return Promise.reject(error);
  }
  return new Promise((resolve, reject) => {
    commandQueue.push({ start, resolve, reject });
    drainCommandQueue();
  });
}

function runCommand(command, args, { input = '', timeout = 0 } = {}) {
  return scheduleCommand(() => new Promise((resolve, reject) => {
    const maxOutputBytes = positiveIntEnv(
      'EIMEMORY_MAX_COMMAND_OUTPUT_BYTES',
      DEFAULT_MAX_COMMAND_OUTPUT_BYTES
    );
    const child = spawn(command, args, {
      stdio: ['pipe', 'pipe', 'pipe'],
      windowsHide: true,
    });
    const stdout = [];
    const stderr = [];
    let outputBytes = 0;
    let settled = false;
    let timer;

    const fail = (error) => {
      if (settled) {
        return;
      }
      settled = true;
      clearTimeout(timer);
      reject(error);
    };
    const collect = (target, chunk) => {
      outputBytes += chunk.length;
      if (outputBytes > maxOutputBytes) {
        const error = new Error(`eimemory command output exceeded ${maxOutputBytes} bytes`);
        error.code = 'EIMEMORY_OUTPUT_LIMIT';
        child.kill('SIGKILL');
        fail(error);
        return;
      }
      target.push(chunk);
    };

    child.stdout.on('data', (chunk) => collect(stdout, chunk));
    child.stderr.on('data', (chunk) => collect(stderr, chunk));
    child.on('error', fail);
    child.on('close', (status, signal) => {
      if (settled) {
        return;
      }
      settled = true;
      clearTimeout(timer);
      const stdoutText = Buffer.concat(stdout).toString('utf-8');
      const stderrText = Buffer.concat(stderr).toString('utf-8');
      if (status !== 0) {
        const error = new Error(
          stderrText || stdoutText || `eimemory command exited with ${status ?? signal ?? 'unknown'}`
        );
        error.status = status;
        error.signal = signal;
        reject(error);
        return;
      }
      resolve({ status, stdout: stdoutText, stderr: stderrText });
    });

    if (timeout > 0) {
      timer = setTimeout(() => {
        const error = new Error(`eimemory command timed out after ${timeout}ms`);
        error.code = 'ETIMEDOUT';
        child.kill('SIGTERM');
        const forceKill = setTimeout(() => child.kill('SIGKILL'), 250);
        forceKill.unref?.();
        fail(error);
      }, timeout);
      timer.unref?.();
    }
    child.stdin.on('error', (error) => {
      if (error?.code !== 'EPIPE') {
        fail(error);
      }
    });
    child.stdin.end(input);
  }));
}

async function invokeHook(api, hook, event) {
  const payload = normalizeEventPayload(hook, event);
  const key = cacheKeyFor('hook', hook, payload);
  const cacheable = hook === 'before_prompt_build';
  if (cacheable) {
    const inflight = hookResultInflight.get(key);
    if (inflight) {
      return await inflight;
    }
  }
  const command = resolveHookCommand();
  const pending = (async () => {
    const result = await runCommand(command[0], [...command.slice(1), hook], {
      input: JSON.stringify(payload),
      timeout: configuredHookTimeout(api, hook, defaultHookTimeoutMs(hook)),
    });
    const parsed = JSON.parse(result.stdout || '{}');
    return parsed;
  })();
  if (cacheable) {
    hookResultInflight.set(key, pending);
  }
  try {
    return await pending;
  } finally {
    if (cacheable && hookResultInflight.get(key) === pending) {
      hookResultInflight.delete(key);
    }
  }
}

async function invokeBridge(api, event) {
  const key = cacheKeyFor('bridge', 'feishu', event);
  pruneHookCache();
  const cached = hookResultCache.get(key);
  if (cached) {
    return cached.value;
  }
  const inflight = hookResultInflight.get(key);
  if (inflight) {
    return await inflight;
  }
  const command = resolveBridgeCommand();
  const pending = (async () => {
    const result = await runCommand(command[0], [...command.slice(1)], {
      input: JSON.stringify(event),
      timeout: configuredBridgeTimeout(api, DEFAULT_BRIDGE_TIMEOUT_MS),
    });
    const parsed = JSON.parse(result.stdout || '{}');
    hookResultCache.set(key, { createdAt: nowMs(), value: parsed });
    return parsed;
  })();
  hookResultInflight.set(key, pending);
  try {
    return await pending;
  } finally {
    if (hookResultInflight.get(key) === pending) {
      hookResultInflight.delete(key);
    }
  }
}

async function invokeCli(args) {
  const command = resolveCliCommand();
  const result = await runCommand(command[0], [...command.slice(1), ...args], {
    timeout: Number(process.env.EIMEMORY_TOOL_TIMEOUT_MS || 30000),
  });
  return JSON.parse(result.stdout || '{}');
}

async function safeInvokeHook(api, hook, event) {
  try {
    const result = await invokeHook(api, hook, event);
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

async function safeInvokeBridge(api, event) {
  try {
    const result = await invokeBridge(api, event);
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
  return COMPLETION_CLAIM_MARKERS.some((pattern) => pattern.test(normalized))
    && UNRESOLVED_COMPLETION_MARKERS.some((pattern) => pattern.test(normalized));
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
  if (!api?.registerTool) {
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
      if (!truthy(process.env.EIMEMORY_ENABLE_E2E_TOOL)) {
        const result = { ok: false, error: 'e2e_tool_disabled' };
        return {
          content: [{ type: 'text', text: JSON.stringify(result) }],
          details: result,
        };
      }
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
        result = await invokeCli(args);
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
    additionalProperties: false,
    properties: {},
  },
  register(api) {
    api?.logger?.info?.('eimemory-bridge: registering OpenClaw hooks');
    registerStatusTool(api);
    registerMemoryE2ETool(api);
    writeReplyDeliveryState(readReplyDeliveryState());
    registerTypedHookOnce(api, 'message_received', async (event, context) => {
      trackReplyInbound(event, context);
      return (await safeInvokeHook(api, 'message_received', mergeHookEventContext(event, context))) || {};
    });
    registerTypedHookOnce(api, 'before_prompt_build', async (event, context) => {
      trackReplyProgress(event, context);
      const contextualEvent = mergeHookEventContext(event, context);
      const correlatedEvent = correlatePendingLoopTask(contextualEvent);
      rememberLoopTask(correlatedEvent, null);
      if (!promptInjectionEnabled(api)) {
        return {};
      }
      const bridgePayload = shouldInvokeBridgeBeforePrompt(api, correlatedEvent)
        ? await safeInvokeBridge(api, normalizeEventPayload('before_prompt_build', correlatedEvent))
        : null;
      const payload = await safeInvokeHook(api, 'before_prompt_build', correlatedEvent);
      rememberLoopTask(correlatedEvent, payload);
      const bridgeContext = buildBridgePrependContext(bridgePayload);
      if (!payload) {
        return bridgeContext ? { prependContext: bridgeContext } : {};
      }
      const bundle = payload.memory_bundle || {};
      const personaContext = buildPersonaGuidanceContext(payload.persona_guidance || bundle?.explanation?.persona_guidance);
      const proactiveContext = typeof payload?.proactive_recall?.context === 'string'
        ? payload.proactive_recall.context.trim()
        : '';
      const hasProactiveDecision = payload.proactive_recall && typeof payload.proactive_recall === 'object';
      const memoryContext = hasProactiveDecision
        ? proactiveContext
        : buildMemoryPrependContext(bundle, payload.injection_plan);
      const prependContext = [bridgeContext, personaContext, memoryContext].filter(Boolean).join('\n\n');
      if (!prependContext) {
        return {};
      }
      const proactive = payload.proactive_recall || {};
      const proactiveTaskContext = payload.task_context || {};
      if (typeof proactive.decision_id === 'string' && proactive.decision_id) {
        await safeInvokeHook(api, 'proactive_injected', {
          ...correlatedEvent,
          decision_id: proactive.decision_id,
          turn_id: proactiveTaskContext.proactive_turn_id || correlatedEvent.turn_id || correlatedEvent.turnId || '',
          source_ids: Array.isArray(proactiveTaskContext.proactive_source_ids)
            ? proactiveTaskContext.proactive_source_ids
            : [],
          injected_citations: Array.from(
            new Set((proactiveContext.match(/pm:[0-9a-f]{20}/g) || []))
          ),
        });
      }
      return { prependContext };
    });
    registerTypedHookOnce(api, 'agent_end', async (event, context) => {
      trackReplyAgentEnd(event, context);
      const correlatedEvent = correlatePendingLoopTask(
        mergeHookEventContext(event, context),
        { terminalKind: 'agent_end' },
      );
      const result = (await safeInvokeHook(api, 'agent_end', correlatedEvent)) || {};
      forgetTerminalLoopTask(correlatedEvent, result);
      return result;
    });
    registerTypedHookOnce(api, 'message_sent', async (event, context) => {
      trackReplyMessageSent(event, context);
    });
    registerTypedHookOnce(api, 'session_end', async (event, context) => {
      const correlatedEvent = correlatePendingLoopTask(mergeHookEventContext(event, context));
      const result = (await safeInvokeHook(api, 'session_end', correlatedEvent)) || {};
      forgetTerminalLoopTask(correlatedEvent, result);
      return result;
    });
    registerTypedHookOnce(api, 'before_agent_finalize', async (event, context) => (
      completionGateRevision(mergeHookEventContext(event, context))
    ));
    registerTypedHookOnce(api, 'before_tool_call', async (event, context) => {
      trackReplyProgress(event, context);
      return completionGateBeforeToolCall(mergeHookEventContext(event, context));
    });
    registerTypedHookOnce(api, 'after_tool_call', async (event, context) => {
      trackReplyProgress(event, context);
      trackReplyMessageToolResult(event, context);
      rememberToolReceipt(mergeHookEventContext(event, context));
    });
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
