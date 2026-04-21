'use strict';

const { spawnSync } = require('node:child_process');

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

function normalizeEventPayload(hook, event) {
  if (hook === 'message_received') {
    const scope = normalizeScope(event);
    const content = normalizeContent(event?.content ?? event?.message?.content);
      return {
        session_id: String(event?.sessionId || event?.session_id || ''),
        ...scope,
        capture_memory: Boolean(event?.capture_memory || event?.captureMemory),
        message: {
          role: String(event?.from || event?.message?.role || 'user'),
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

function normalizeScope(event, metadata = {}) {
  return {
    tenant_id: String(event?.tenantId || event?.tenant_id || 'default'),
    agent_id: String(event?.agentId || event?.agent_id || 'main'),
    workspace_id: String(event?.workspaceId || event?.workspace_id || ''),
    user_id: String(
      event?.userId
      || event?.user_id
      || event?.senderId
      || event?.sender_id
      || metadata.sender_id
      || metadata.sender
      || ''
    ),
  };
}

function extractPromptMetadata(prompt) {
  const merged = {};
  const text = String(prompt || '');
  const fencePattern = /```(?:json)?\s*([\s\S]*?)```/gi;
  let match = fencePattern.exec(text);
  while (match) {
    try {
      const parsed = JSON.parse(match[1]);
      if (parsed && typeof parsed === 'object' && !Array.isArray(parsed)) {
        Object.assign(merged, parsed);
      }
    } catch (_error) {
      // Metadata is best-effort; malformed prompt wrappers should never block recall.
    }
    match = fencePattern.exec(text);
  }
  const messageMatch = text.match(/\[msg:([^\]]+)\]/i);
  if (messageMatch && !merged.message_id) {
    merged.message_id = messageMatch[1];
  }
  return merged;
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

function safeInvokeHook(hook, event) {
  try {
    return invokeHook(hook, event);
  } catch (_error) {
    return null;
  }
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
    api.on('message_received', async (event) => safeInvokeHook('message_received', event) || {});
    api.on('before_prompt_build', async (event) => {
      const payload = safeInvokeHook('before_prompt_build', event);
      if (!payload) {
        return {};
      }
      const bundle = payload.memory_bundle || {};
      const items = Array.isArray(bundle.items) ? bundle.items : [];
      if (!items.length) {
        return {};
      }
      const context = items
        .map((item) => {
          const summary = cleanInjectedMemoryText(item.summary || item.content?.text || '');
          if (!summary) {
            return '';
          }
          return `- ${item.title}: ${summary}`.trim();
        })
        .filter(Boolean)
        .join('\n');
      if (!context) {
        return {};
      }
      return { prependContext: `Relevant eimemory context:\n${context}` };
    });
    api.on('agent_end', async (event) => safeInvokeHook('agent_end', event) || {});
  },
};

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
