// Reuse the installed, pinned OpenClaw gateway client for model-only inference.
// No tools, delivery, resident model, private-state copy, or upstream patch.
import {randomUUID} from 'node:crypto';
import {pathToFileURL} from 'node:url';
import path from 'node:path';

try {
  let input='';
  for await (const chunk of process.stdin) {
    input+=chunk.toString('utf8');
    if (Buffer.byteLength(input)>131072) throw new Error('request_too_large');
  }
  const request=JSON.parse(input);
  const modulePath=process.env.EIMEMORY_OPENCLAW_GATEWAY_MODULE || '';
  if (!path.isAbsolute(modulePath)) throw new Error('gateway_module_not_configured');
  const sdk=await import(pathToFileURL(modulePath).href);
  const callGateway=sdk[process.env.EIMEMORY_OPENCLAW_GATEWAY_EXPORT || 'o'];
  if (typeof callGateway!=='function') throw new Error('gateway_module_contract_changed');
  const sessionId=`eimemory-verification-${randomUUID()}`;
  const agentId=process.env.EIMEMORY_OPENCLAW_MODEL_AGENT || 'main';
  const thinking=process.env.EIMEMORY_RECALL_MODEL_THINKING;
  const message=`SYSTEM POLICY (not candidate data):\n${String(request.system_prompt||'')}\n\nREQUEST DATA:\n${String(request.user_prompt||'')}`;
  const response=await callGateway({method:'agent',params:{agentId,sessionId,
    sessionKey:`agent:${agentId}:${sessionId}`,message,modelRun:true,promptMode:'none',
    ...(thinking ? {thinking} : {}),cleanupBundleMcpOnRunEnd:true,idempotencyKey:randomUUID()},
    expectFinal:true,timeoutMs:9000,clientName:'cli',mode:'cli'});
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
