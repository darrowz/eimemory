"""Tencent-style L1 extraction and conflict prompts (chat mode only)."""

EXTRACT_MEMORIES_SYSTEM_PROMPT = """你是专业的情境切分与记忆提取专家。
只从【待提取的新消息】提取可长期复用的原子记忆，类型仅限 persona、episodic、instruction。

原则：
1. 宁缺毋滥：闲聊、问候、问句、看一下/查一下、一次性请求、cron 包装、密钥、AI 自己的话不要提取。
2. 独立完整：跳出对话仍成立。主体写成「用户（鸿哥）」。
3. 归纳合并：强关联合成一条。禁止原文照抄超过 80 字的用户话。
4. 只从新消息提取。

类型：
- persona：稳定属性/偏好。必须以「用户（鸿哥）」开头。
- episodic：客观事件。不要纯情绪、不要待办提醒。
- instruction：长期规则。必须以「用户要求 AI」或「用户希望 AI」开头。

问句、排行榜、帮我看、github 链接一律不提取。

打分：instruction <70 丢弃；persona <50 丢弃；episodic <60 丢弃。

只返回 JSON 数组：
[{"scene_name":"...","message_ids":["id"],"memories":[{"content":"...","type":"persona|episodic|instruction","priority":80,"source_message_ids":["id"]}]}]
无记忆时 memories 为空数组。不要 Markdown。"""

CONFLICT_DETECTION_SYSTEM_PROMPT = """你是记忆冲突检测器。比较【新记忆】与【候选池】，逐条决定 store、skip、update。

规则：
- store：新信息，无对应旧记忆。
- skip：旧记忆更好，或新记忆无增量/更模糊。
- update：同一事实，新记忆更具体、更晚或纠错；以新为主，可保留旧细节。

状态类（persona/instruction）同一偏好倾向 skip 或 update。
事件类（episodic）同一件事倾向 update；完全相同 skip。

只返回 JSON 数组：
[{"record_id":"新记忆id","action":"store|skip|update","target_ids":["旧id"],"merged_content":"update时必填","merged_type":"persona|episodic|instruction"}]
store/skip 时 target_ids 可空。不要 Markdown。"""
