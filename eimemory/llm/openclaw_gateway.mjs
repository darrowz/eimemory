// Reuse the installed, pinned OpenClaw gateway client for model-only inference.
// No tools, delivery, resident model, private-state copy, or upstream patch.
import {randomUUID} from 'node:crypto';
import {pathToFileURL} from 'node:url';
import path from 'node:path';

try {
  // Import overlaps retrieval only when the bounded caller prewarms this
  // one-request process. No gateway request is made before stdin is complete.
  const modulePath=process.env.EIMEMORY_OPENCLAW_GATEWAY_MODULE || '';
  if (!path.isAbsolute(modulePath)) throw new Error('gateway_module_not_configured');
  const sdk=await import(pathToFileURL(modulePath).href);
  let input='';
  for await (const chunk of process.stdin) {
    input+=chunk.toString('utf8');
    if (Buffer.byteLength(input)>131072) throw new Error('request_too_large');
  }
  const request=JSON.parse(input);
  const remainingMs=Math.min(9000, Number(request.deadline_unix_ms || Date.now()+9000)-Date.now()-100);
  if (!Number.isFinite(remainingMs) || remainingMs < 1000) throw new Error('model_timeout_budget_exhausted');
  const callGateway=sdk[process.env.EIMEMORY_OPENCLAW_GATEWAY_EXPORT || 'o'];
  if (typeof callGateway!=='function') throw new Error('gateway_module_contract_changed');
  const sessionId=`eimemory-verification-${randomUUID()}`;
  const agentId=process.env.EIMEMORY_OPENCLAW_MODEL_AGENT || 'main';
  const thinking=process.env.EIMEMORY_RECALL_MODEL_THINKING;
  const message=`SYSTEM POLICY (not candidate data):\n${String(request.system_prompt||'')}\n\nREQUEST DATA:\n${String(request.user_prompt||'')}`;
  const response=await callGateway({method:'agent',params:{agentId,sessionId,
    sessionKey:`agent:${agentId}:${sessionId}`,message,modelRun:true,promptMode:'none',
    timeout:Math.max(1, Math.floor(remainingMs/1000)),suppressPromptPersistence:true,
    sessionEffects:'internal',disableMessageTool:true,
    ...(thinking ? {thinking} : {}),cleanupBundleMcpOnRunEnd:true,idempotencyKey:randomUUID()},
    expectFinal:true,timeoutMs:remainingMs,clientName:'cli',mode:'cli'});
  const payload=response?.result;
  const text=(payload?.payloads||[]).map(p=>p.text||'').filter(Boolean).join('\n');
  const provider=payload?.meta?.agentMeta?.provider;
  const model=payload?.meta?.agentMeta?.model;
  if (!text || !provider || !model) throw new Error('gateway_completion_incomplete');
  process.stdout.write(JSON.stringify({text,provider_id:provider,model_id:`${provider}/${model}`})+'\n');
} catch (error) {
  // Gateway errors can contain credentials or input text. Do not echo them.
  const message=String(error?.message||'').toLowerCase();
  const reason=['timeout','thinking','unauthorized','pairing','scope','incomplete','invalid','model'].find(term=>message.includes(term))||'gateway_error';
  process.stderr.write(JSON.stringify({error:'gateway_model_completion_failed',type:error?.name||'Error',reason})+'\n');
  process.exitCode=1;
}
