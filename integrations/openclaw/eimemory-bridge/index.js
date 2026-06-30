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
      query: cleanPromptQuery(rawQuery),
      raw_query: rawQuery,
      task_context: normalizeRecallContext(event?.task_context || event?.taskContext || {}),
    };
  }
  const scope = normalizeScope(event);
  const rawQuery = String(event?.query || event?.prompt || event?.userPhrase || event?.user_phrase || '');
  const taskContext = normalizeRecallContext(event?.task_context || event?.taskContext || {});
  const outcome = normalizeObject(event?.outcome);
  return {
    session_id: normalizeSessionId(event),
    ...scope,
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
      verified: outcome.verified === true || event?.verified === true || event?.verification_status === 'verified',
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

function invokeHook(hook, event) {
  const command = resolveHookCommand();
  const result = spawnSync(command[0], [...command.slice(1), hook], {
    input: JSON.stringify(normalizeEventPayload(hook, event)),
    encoding: 'utf-8',
    timeout: Number(process.env.EIMEMORY_HOOK_TIMEOUT_MS || 15000),
  });
  if (result.error) {
    throw result.error;
  }
  if (result.status !== 0) {
    throw new Error(result.stderr || result.stdout || `eimemory hook ${hook} failed`);
  }
  return JSON.parse(result.stdout || '{}');
}

function invokeBridge(event) {
  const command = resolveBridgeCommand();
  const result = spawnSync(command[0], [...command.slice(1)], {
    input: JSON.stringify(event),
    encoding: 'utf-8',
    timeout: Number(process.env.EIMEMORY_BRIDGE_TIMEOUT_MS || 5000),
  });
  if (result.error) {
    throw result.error;
  }
  if (result.status !== 0) {
    throw new Error(result.stderr || result.stdout || 'ei-bridge feishu failed');
  }
  return JSON.parse(result.stdout || '{}');
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
    const result = invokeHook(hook, event);
    api?.logger?.info?.(`eimemory-bridge: ${hook} completed`);
    return result;
  } catch (error) {
    api?.logger?.warn?.(`eimemory-bridge: ${hook} failed: ${error?.message || String(error)}`);
    return null;
  }
}

function safeInvokeBridge(api, event) {
  try {
    const result = invokeBridge(event);
    api?.logger?.info?.('eimemory-bridge: ei-bridge feishu completed');
    return result;
  } catch (error) {
    api?.logger?.warn?.(`eimemory-bridge: ei-bridge feishu failed: ${error?.message || String(error)}`);
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

function truthy(value) {
  return /^(1|true|yes|on)$/i.test(String(value || '').trim());
}

function readOpenClawPromptInjectionPolicy() {
  try {
    const configPath = process.env.OPENCLAW_CONFIG_PATH
      || path.join(process.env.OPENCLAW_STATE_DIR || path.join(os.homedir(), '.openclaw'), 'openclaw.json');
    const config = JSON.parse(fs.readFileSync(configPath, 'utf8'));
    return config?.plugins?.entries?.['eimemory-bridge']?.hooks?.allowPromptInjection === true;
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
      const result = invokeCli(args);
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
    registerTypedHook(api, 'message_received', async (event) => safeInvokeHook(api, 'message_received', event) || {});
    if (promptInjectionEnabled(api) || usesLegacyHookApi(api)) {
      registerTypedHook(api, 'before_prompt_build', async (event) => {
        const bridgePayload = safeInvokeBridge(api, normalizeEventPayload('before_prompt_build', event));
        const payload = safeInvokeHook(api, 'before_prompt_build', event);
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
    registerTypedHook(api, 'agent_end', async (event) => safeInvokeHook(api, 'agent_end', event) || {});
    registerTypedHook(api, 'session_end', async (event) => safeInvokeHook(api, 'session_end', event) || {});
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
  return cleaned;
}
