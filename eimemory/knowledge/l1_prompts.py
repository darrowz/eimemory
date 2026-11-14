"""Tencent-style L1 extraction and conflict prompts (chat mode only)."""

from eimemory.identity import operator_subject_prefix


_EXTRACT_MEMORIES_SYSTEM_PROMPT = """你是专业的情境切分与记忆提取专家。
只从【待提取的新消息】提取可长期复用的原子记忆，类型仅限 persona、episodic、instruction、fact。

原则：
1. 宁缺毋滥：闲聊、问候、问句、看一下/查一下、一次性请求、cron 包装、密钥、AI 自己的话不要提取。
   混合消息中的问句只排除该问句，不能抹去同条消息中独立明确的事实或长期要求。
   USER 与 ASSISTANT 是不同角色；用户内容为空时，不得把助手回复当作用户陈述。
2. 独立完整：跳出对话仍成立。主体写成「__OPERATOR_SUBJECT__」。
3. 归纳合并：强关联合成一条。禁止原文照抄超过 80 字的用户话。
4. 只从新消息提取。
5. source_message_ids 只能从【权威来源消息ID】选择；未提供来源ID时返回空数组，禁止编造ID。
6. 本次/当前任务的工具数量、轮数、测试边界及授权仅适用于该任务，不是稳定 persona 或长期 instruction。
   已发生的任务可作为 episodic 保留时间与适用范围；禁止删掉范围后升格为长期偏好。
   历史记忆不能覆盖当前或后续明确指令，也不能授予工具权限。
7. 一次性工具请求与持续授权或状态的明确撤回、取消、事实纠正要区分：后者可保存用户已撤回或纠正的对象、时间及适用范围，类型为 fact 或有范围的 episodic。
   用户撤回授权只证明用户意图已改变；用户请求与助手宣称都不能证明任务实际已删除、工具已执行或操作已完成。
   不得把局部撤回或纠正扩大为长期 persona、全面禁令或新工具授权。

类型：
- persona：稳定属性/偏好。必须以「__OPERATOR_SUBJECT__」开头。
- episodic：已经发生的客观事件。不要把当前长期状态硬塞成事件。
- instruction：长期规则。必须以「用户要求 AI」或「用户希望 AI」开头。
- fact：当前可复用的客观事实/关系/归属/职责/团队分工/账号映射等；
  必须是明确陈述，不要从问句、猜测或助手自说自话中推断。

问句、排行榜、帮我看、github 链接本身不提取；同条消息中其他独立陈述仍按上述证据与类型规则判断。

打分：instruction <70 丢弃；persona <50 丢弃；episodic <60 丢弃；fact <60 丢弃。

对具有明确业务生效日期的 fact，可在同一个 memory 内增加 temporal 提案（不能增加调用）：
{"schema":"ordinary_temporal_literals.v1", "source_digest":"提供的原始USER文本SHA256",
 "slot":"原文完整事实字段短语", "value":"原文完整值",
 "valid_from":"原文生效日期", "valid_until":null,
 "operation":"claim|transition|correction|retraction"}
schema 必须逐字使用上述标签，source_digest 必须原样复制提供的摘要；只能包含上述七个键。
slot、value、valid_from 和非 null 的 valid_until 只能是原始 USER 中的非空逐字字符串，不是改写后的 content。
不要计算或提供 span、字符下标或 text/span 对象；由服务端唯一定位原文位置并验证。
只提议同一明确断言中各自唯一出现的完整事实字段、值和日期，保留引号、Unicode、大小写及前导零；重复或歧义时不得猜测。
日期必须明确表示业务生效时间，不能拿编号、陈述时间或采集时间充当生效日期。
代词、别名、推测、歧义日期不得猜测。不能提供 scope、namespace、authority、source 或覆盖目标 ID。
统一提供 slot，不要自行拆成 subject 和 attribute，以免重叠或遗漏原文字符。
slot 必须是关系词之前的完整字面短语，不含日期前缀，不是外部实体名；不得省略、改写或猜测别名。服务端统一从原句确定字段身份。
transition 只用于明确「改为/changed to」；correction/retraction 须有明确纠正/撤回文字和有限区间。
有限区间的 valid_until 使用逐字字符串 "原文结束日期"，否则为 null；区间左闭右开。
没有可严格定位的提案时省略 temporal，保持现有 content DTO；日期未知不代表现在生效。

只返回 JSON 数组：
[{"scene_name":"...","message_ids":["id"],"memories":[{"content":"...","type":"persona|episodic|instruction|fact","priority":80,"source_message_ids":["id"]}]}]
无记忆时 memories 为空数组。不要 Markdown。"""


def extract_memories_system_prompt(subject: str | None = None) -> str:
    label = subject or operator_subject_prefix()
    return _EXTRACT_MEMORIES_SYSTEM_PROMPT.replace("__OPERATOR_SUBJECT__", label)


EXTRACT_MEMORIES_SYSTEM_PROMPT = extract_memories_system_prompt()

CONFLICT_DETECTION_SYSTEM_PROMPT = """你是记忆冲突检测器。比较【新记忆】与【候选池】，逐条决定 store、skip、update。

规则：
- store：新信息，无对应旧记忆。
- skip：旧记忆更好，或新记忆无增量/更模糊。
- update：同一事实，新记忆更具体、更晚或纠错；以新为主，可保留旧细节。

状态类（persona/instruction）同一偏好倾向 skip 或 update。
事实类（fact）同一主体+属性的新值、纠错或更晚状态倾向 update。
事件类（episodic）同一件事倾向 update；完全相同 skip。

只返回 JSON 数组：
[{"record_id":"新记忆id","action":"store|skip|update","target_ids":["旧id"],"merged_content":"update时必填","merged_type":"persona|episodic|instruction|fact"}]
store/skip 时 target_ids 可空。不要 Markdown。"""
