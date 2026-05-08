'use strict';

const { spawnSync } = require('node:child_process');

const HONGTU_AGENT_ID = 'hongtu';
const HONGTU_WORKSPACE_ID = 'embodied';
const DEFAULT_OPERATOR_USER_ID = 'darrow';

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
      task_context: Object.assign({}, event?.task_context || event?.taskContext || {}),
    };
  }
  const scope = normalizeScope(event);
  return {
    session_id: normalizeSessionId(event),
    ...scope,
    assistant_messages: Array.isArray(event?.messages)
      ? event.messages
          .filter((message) => String(message?.role || '').toLowerCase() === 'assistant')
          .map((message) => ({ content: normalizeContent(message?.content) }))
      : [],
    outcome: {
      success: event?.success !== false,
      notes: String(event?.error || ''),
    },
  };
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
    agent_id: HONGTU_AGENT_ID,
    workspace_id: HONGTU_WORKSPACE_ID,
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
    },
    async execute() {
      return JSON.stringify({
        ok: true,
        hookCommandConfigured: Boolean((process.env.EIMEMORY_HOOK_COMMAND || '').trim()),
        bridgeCommandConfigured: Boolean((process.env.EIMEMORY_BRIDGE_COMMAND || '').trim()),
      });
    },
  }), { name: 'eimemory_bridge_status' });
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
    registerTypedHook(api, 'message_received', async (event) => safeInvokeHook(api, 'message_received', event) || {});
    registerTypedHook(api, 'before_prompt_build', async (event) => {
      const bridgePayload = safeInvokeBridge(api, normalizeEventPayload('before_prompt_build', event));
      const payload = safeInvokeHook(api, 'before_prompt_build', event);
      const bridgeContext = buildBridgePrependContext(bridgePayload);
      if (!payload) {
        return bridgeContext ? { prependContext: bridgeContext } : {};
      }
      const bundle = payload.memory_bundle || {};
      const items = Array.isArray(bundle.items) ? bundle.items : [];
      if (!items.length) {
        return bridgeContext ? { prependContext: bridgeContext } : {};
      }
      const memoryContext = buildMemoryPrependContext(items);
      if (!memoryContext && !bridgeContext) {
        return {};
      }
      return { prependContext: [bridgeContext, memoryContext].filter(Boolean).join('\n\n') };
    });
    registerTypedHook(api, 'agent_end', async (event) => safeInvokeHook(api, 'agent_end', event) || {});
  },
};

function buildBridgePrependContext(payload) {
  if (!payload || payload.matched !== true) {
    return '';
  }
  const context = cleanInjectedMemoryText(payload.prepend_context || payload.reply || '');
  return context ? `Live eibrain context:\n${context}` : '';
}

function buildMemoryPrependContext(items) {
  const context = items
    .map((item) => {
      if (isBridgeAuditMemory(item)) {
        return '';
      }
      const summary = cleanInjectedMemoryText(item.summary || item.content?.text || '');
      if (!summary) {
        return '';
      }
      return `- ${item.title}: ${summary}`.trim();
    })
    .filter(Boolean)
    .join('\n');
  return context ? `Relevant eimemory context:\n${context}` : '';
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
