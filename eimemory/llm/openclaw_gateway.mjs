// Reuse the installed, pinned OpenClaw gateway client for model-only inference.
// No tools, delivery, resident model, private-state copy, or upstream patch.
import {randomUUID} from 'node:crypto';
import {pathToFileURL} from 'node:url';
import path from 'node:path';
import {readFile} from 'node:fs/promises';
import {createInterface} from 'node:readline';

async function callWithClient(sdk, options) {
  const configPath=process.env.EIMEMORY_OPENCLAW_GATEWAY_CONFIG || '';
  if (!path.isAbsolute(configPath)) throw new Error('gateway_config_not_configured');
  const config=JSON.parse(await readFile(configPath, 'utf8'));
  const token=config?.gateway?.auth?.token;
  const port=config?.gateway?.port || 18789;
  if (typeof token!=='string' || !token || !Number.isInteger(port) || port<1 || port>65535)
    throw new Error('gateway_auth_configuration_invalid');
  const GatewayClient=sdk[process.env.EIMEMORY_OPENCLAW_GATEWAY_EXPORT || 't'];
  if (typeof GatewayClient!=='function') throw new Error('gateway_module_contract_changed');
  let client, timer;
  let stage='gateway_connect';
  const started=Date.now();
  try {
    return await new Promise((resolve,reject)=>{
      timer=setTimeout(()=>reject(new Error('gateway_timeout')),options.timeoutMs);
      client=new GatewayClient({url:`ws://127.0.0.1:${port}`,token,
        clientName:'cli',mode:'cli',role:'operator',scopes:['operator.write'],
        minProtocol:4,maxProtocol:4,sharedStateMode:'read-only',
        onHelloOk:()=>{
          stage='gateway_response';
          client.request(options.method,options.params,
            {expectFinal:true,timeoutMs:options.timeoutMs}).then(resolve,reject);
        },
        onClose:()=>reject(new Error('gateway_connection_closed')),
        onConnectError:()=>reject(new Error('gateway_auth_connection_failed'))});
      client.start();
    });
  } catch (error) {
    error.gatewayStage=stage;
    error.gatewayElapsedMs=Date.now()-started;
    throw error;
  } finally {
    clearTimeout(timer);
    client?.stop();
  }
}

async function initialize() {
  // Import overlaps retrieval only when the bounded caller prewarms this
  // one-request process. No gateway request is made before stdin is complete.
  const modulePath=process.env.EIMEMORY_OPENCLAW_GATEWAY_MODULE || '';
  if (!path.isAbsolute(modulePath)) throw new Error('gateway_module_not_configured');
  const sdk=await import(pathToFileURL(modulePath).href);
  return sdk;
}

async function complete(sdk, request) {
  const remainingMs=Math.min(9000, Number(request.deadline_unix_ms || Date.now()+9000)-Date.now()-100);
  if (!Number.isFinite(remainingMs) || remainingMs < 1000) throw new Error('model_timeout_budget_exhausted');
  const callGateway=process.env.EIMEMORY_OPENCLAW_GATEWAY_MODE==='client'
    ? options=>callWithClient(sdk,options)
    : sdk[process.env.EIMEMORY_OPENCLAW_GATEWAY_EXPORT || 'o'];
  if (typeof callGateway!=='function') throw new Error('gateway_module_contract_changed');
  const sessionId=`eimemory-verification-${randomUUID()}`;
  const agentId=process.env.EIMEMORY_OPENCLAW_MODEL_AGENT || 'main';
  const thinking=process.env.EIMEMORY_RECALL_MODEL_THINKING;
  const message=`SYSTEM POLICY (not candidate data):\n${String(request.system_prompt||'')}\n\nREQUEST DATA:\n${String(request.user_prompt||'')}`;
  let response;
  try {
    response=await callGateway({method:'agent',params:{agentId,sessionId,
    sessionKey:`agent:${agentId}:${sessionId}`,message,modelRun:true,promptMode:'none',
    // Gateway accepts whole seconds. The millisecond client deadline remains
    // authoritative; rounding down would discard almost a second of budget.
    timeout:Math.max(1, Math.ceil(remainingMs/1000)),disableMessageTool:true,
    ...(thinking ? {thinking} : {}),cleanupBundleMcpOnRunEnd:true,idempotencyKey:randomUUID()},
      expectFinal:true,timeoutMs:remainingMs,clientName:'cli',mode:'cli'});
  } catch (error) {
    error.verificationSessionId=sessionId;
    throw error;
  }
  const payload=response?.result;
  const text=(payload?.payloads||[]).map(p=>p.text||'').filter(Boolean).join('\n');
  const provider=payload?.meta?.agentMeta?.provider;
  const model=payload?.meta?.agentMeta?.model;
  if (!text || !provider || !model) throw new Error('gateway_completion_incomplete');
  return {text,provider_id:provider,model_id:`${provider}/${model}`};
}

function safeError(error) {
  // Gateway errors can contain credentials or input text. Do not echo them.
  const message=String(error?.message||'').toLowerCase();
  const reason=['timeout','thinking','unauthorized','pairing','scope','incomplete','invalid','model',
    'sessioneffects','suppressprompt','permission','forbidden','internal','deliver'].find(term=>message.includes(term))||'gateway_error';
  return {error:'gateway_model_completion_failed',type:error?.name||'Error',reason,
    ...(['gateway_connect','gateway_response'].includes(error?.gatewayStage)
      ? {gateway_stage:error.gatewayStage} : {}),
    ...(Number.isFinite(error?.gatewayElapsedMs)
      ? {gateway_elapsed_ms:Math.max(0,Math.min(10000,error.gatewayElapsedMs))} : {}),
    ...(/^eimemory-verification-[a-f0-9-]{36}$/.test(error?.verificationSessionId||'')
      ? {verification_session_id:error.verificationSessionId} : {})};
}

try {
  const sdk=await initialize();
  if (process.argv.includes('--serve')) {
    const lines=createInterface({input:process.stdin,crlfDelay:Infinity});
    let idle=setTimeout(()=>process.exit(0),120000);
    idle.unref();
    for await (const line of lines) {
      clearTimeout(idle);
      let request;
      try {
        if (Buffer.byteLength(line)>131072) throw new Error('request_too_large');
        request=JSON.parse(line);
        if (!/^[a-f0-9]{32}$/.test(request.request_id||'')) throw new Error('request_identity_invalid');
        const result=await complete(sdk,request);
        process.stdout.write(JSON.stringify({request_id:request.request_id,result})+'\n');
      } catch (error) {
        process.stdout.write(JSON.stringify({request_id:request?.request_id||'',...safeError(error)})+'\n');
      }
      idle=setTimeout(()=>process.exit(0),120000);
      idle.unref();
    }
    clearTimeout(idle);
  } else {
    let input='';
    for await (const chunk of process.stdin) {
      input+=chunk.toString('utf8');
      if (Buffer.byteLength(input)>131072) throw new Error('request_too_large');
    }
    process.stdout.write(JSON.stringify(await complete(sdk,JSON.parse(input)))+'\n');
  }
} catch (error) {
  process.stderr.write(JSON.stringify(safeError(error))+'\n');
  process.exitCode=1;
}
