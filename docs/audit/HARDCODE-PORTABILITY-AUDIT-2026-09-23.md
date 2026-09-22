

eimemory 硬编码与可移植性审计报告（第四轮）

 

  
Code Audit · Portability

  
eimemory 硬编码与可移植性审计报告

  

    

轮次
　第四轮

    

基线
　
6b5026a
 / 1.13.18

    

日期
　2026-09-23

    

范围
　387 个 .py / 175,990 行

    

主题
　跨服务器 · 跨大模型适配

  

  

    

核心结论：项目已具备完整的可配置设计，但该设计只覆盖了适配器平面，未贯穿治理平面与部署平面。

    
项目内置 130+ 个环境变量、独立的 
eimemory/config/
 配置层，以及供应商中立的 LLM 命令协议——可移植性的
基础设施是健全的
。问题在于：治理平面与部署平面大量使用
模块级常量
承载作者私有环境事实，形成「可配置外壳 + 写死内核」的结构。更关键的是，安装脚本会导出 
EIMEMORY_AGENT_ID
 等身份变量并被适配器层读取，
但身份层 
identity.py
 完全不读它们
——配置通道处于半接通状态。

  

  

    

5

P0 阻断
无法运行 / 私密泄露

    

7

P1 严重
功能永久失效

    

3

P2 一般
文档与命名污染

    

247

硬编码实例
（源码 130 / 部署 108 / 其他 9）

    

0

硬编码凭据
扫描零命中

  

  
实例分布

  

    

区域

实例数

主要形态

    

      

eimemory/
 身份字面量

60

darrow / hongtu / honjia / honxin / eibrain / embodied

      

eimemory/
 绝对路径字面量

70

/dev-project/eimemory · /opt/eimemory · /var/lib/eimemory · /etc/eimemory

      

deploy/

108

darrow · /home/darrow · 100.105.189.120 · 硬编码单元文件

      

docs/deployment.md

19

用户照抄的部署指令全部指向作者环境

      

integrations/
 · 
scripts/
 · 
examples/

9 + 更多

交付给用户的桥接插件、示例数据集

    

  

  
P0 · 阻断级发现

  

    

P0

HC-01
信任锚点写死作者开发路径与分支

    

      

eimemory/governance/code_evolution_effects.py:47-49

TRUSTED_REPOSITORY_ROOT = Path(
"/dev-project/eimemory"
)

TRUSTED_REMOTE = 
"origin"

TRUSTED_BRANCH = 
"master"

      
这三个模块级常量在
同一文件内被 40+ 处强制校验
引用，全部为硬相等判断：

      

:713
  
if
 repository_root.resolve() != TRUSTED_REPOSITORY_ROOT.resolve():  
→ 拒绝

:1285
 
if
 root.resolve() != TRUSTED_REPOSITORY_ROOT.resolve():           
→ 拒绝

:715
  
if
 str(transaction.get(
"repository_remote"
) 
or
 
""
) != TRUSTED_REMOTE:  
→ 拒绝

:717
  
if
 ...removeprefix(
"refs/heads/"
) != TRUSTED_BRANCH:                
→ 拒绝

      

后果：
任何将仓库部署在 
/dev-project/eimemory
 之外的用户，其
整个代码自演化平面
（候选物化、worktree、push、部署、回滚）永久失效；使用 
main
 分支的用户同样直接失败。无环境变量旁路。

    

  

  

    

P0

HC-02
硬编码作者 Tailscale 私网 IP 
100.105.189.120

    

      

        
deploy/systemd/eimemory-rpc.service:26

        ExecStart=... serve-eibrain-rpc 
--host 100.105.189.120
 --port 8091

        
eimemory/ops/openclaw_loop.py:1015-1016

        os.environ.get(
"EIMEMORY_HEALTH_URL"
, 
"http://100.105.189.120:8091/health"
)

        os.environ.get(
"OPENCLAW_HEALTH_URL"
, 
"http://100.105.189.120:18789/health"
)
      

      
该地址落在 Tailscale/CGNAT 保留段 
100.64.0.0/10
，是作者私有网络节点。

      

四重后果：
① 该单元文件是 
docs/deployment.md:128
 指导用户直接 
cp
 的，他人机器上不可绑定，服务启动即失败；② 健康检查默认值指向作者节点，未配置用户遭遇
沉默超时
而非明确的配置缺失错误；③ 公开了作者私有网络拓扑；④ 
测试套件在固化该缺陷
——
tests/test_deployment_tools.py:445
 断言 
"--host 100.105.189.120 --port 8091" in unit_text
。

    

  

  

    

P0

HC-03
硬编码部署账号 
darrow
 与家目录 
/home/darrow

    

      

        

文件

行

内容

        

          

deploy/systemd/eimemory-rpc.service

10

Environment=HOME=/home/darrow

          

deploy/systemd/eimemory-l1-extract.service

10

Environment=HOME=/home/darrow

          

deploy/systemd/eimemory-l5-fused-closure.service

9,10,13,16,17

HOME
 / 
USER=darrow
 / 日志路径

          

deploy/install_immutable_release.sh

8,39

SERVICE_USER="${SERVICE_USER:-darrow}"

          

deploy/run_memory_l5_fused_closure.sh

3,12,13,40,48,67

export HOME=/home/darrow
 · 
--scope-user darrow

          

deploy/verify_hermes_integration.py

116

get("EIMEMORY_USER_ID", "darrow")

          

deploy/luna_bridge/luna_review_command.py

10

sys.path.insert(0, '/home/darrow/...')

        

      

      

Environment=HOME=/home/darrow
 在用户机上指向不存在的目录，systemd 用户单元启动即失败；
StandardOutput=append:/home/darrow/...
 因目录不存在导致日志写入失败。

      

项目内部已有正例：

deploy/governance.env.example:4
 使用 
/home/USER/.local/bin/openclaw
 占位符写法——正确范式存在，只是未统一应用。

    

  

  

    

P0

HC-04

identity.py
 身份常量零覆盖通道，且泄露真实飞书 OpenID

    

      

eimemory/identity.py:10-25

HONGTU_AGENT_ID = 
"hongtu"

HONGTU_WORKSPACE_ID = 
"embodied"

DEFAULT_OPERATOR_USER_ID = 
"darrow"

FEISHU_DARROW_OPEN_ID = 
"ou_644f810515d8ae7789de6a932d4de854"

HONGTU_SUBJECT_ID = 
"hongtu:darrow"

      

决定性证据：

grep -n "environ\|getenv" eimemory/identity.py
 → 
零命中
。该模块不读取任何环境变量，上述常量完全不可覆盖。

      
而这四个变量
已被适配器平面正确消费
：

      

        

eimemory/adapters/codex/hook.py:55,59,60,62

        

eimemory/adapters/hermes/provider_core.py:1359-1376

        

deploy/install_immutable_release.sh:1831-1833
（安装时导出）

      

      

后果：
安装脚本设置了用户身份，适配器层生效，
身份/作用域层却仍按作者身份计算
，导致作用域漂移与 
is_hongtuish_scope
 误判。这比「完全没做参数化」更危险——安装脚本声称已参数化。

另：

FEISHU_DARROW_OPEN_ID
 是作者真实飞书用户标识，硬编码进公开仓库源码，属个人身份标识泄露。

    

  

  

    

P0

HC-05

_hardware_node_from_record
 函数退化为常量

    

      

eimemory/identity.py:410-418

def
 _hardware_node_from_record(record, *, previous_scope) -> str:
    
if
 previous_scope.agent_id 
in
 {
"honjia"
, 
"honxin"
}:
        
return
 previous_scope.agent_id
    lowered_source = str(record.source 
or
 
""
).lower()
    
if
 lowered_source.startswith(
"openclaw."
) 
or
 lowered_source.startswith(
"eimemory."
):
        
return
 
"honxin"

    
if
 lowered_source.startswith(
"eibrain."
):
        
return
 
"honxin"

    
return
 
"honxin"
          
← 三条分支返回值完全相同

      
函数等价于 
return "honxin"
。这不是风格问题，而是
逻辑已死
：任何 source 前缀（含未匹配的第三分支）都落到 
"honxin"
。同类：
adapters/eibrain/rpc.py:181
、
adapters/openclaw/hooks.py:653
 的 
or "honxin"
；
identity.py:186-187
 的 
hardware_role="head"
 / 
hardware_id="head-v0"
（作者硬件命名）。

    

  

  
P1 · 严重级发现

  

    

P1

HC-06
研究闭环复核模型白名单写死 OpenAI 系列

    

      

eimemory/intake/closure_review.py:16-25

DEFAULT_REVIEW_MODEL = 
"gpt-5.5"
        
# closure.py:11

ALLOWED_REVIEW_MODELS = frozenset({
    DEFAULT_REVIEW_MODEL, 
"gpt-4.1"
, 
"gpt-4.1-mini"
,
    
"gpt-4o"
, 
"gpt-4o-mini"
, 
"o3"
, 
"o3-mini"
, 
"o4-mini"
})

:28-32
  
→ 白名单外抛 ValueError(f"review_model_not_allowed:{candidate}")

      

后果：
使用 DeepSeek / Qwen / GLM / 本地 Ollama 的用户
无法使用研究闭环复核路径
，直接硬失败。这与项目「供应商中立」的核心设计目标
直接自相矛盾
——LLM 调用层已通过 JSON argv 协议做到供应商中立，其上层却按模型名白名单拒绝。

同类：
intake/autonomous_sources.py:508
、
api/runtime.py:351
。

    

  

  

    

P1

HC-07
部署契约三要素写死，形成晋级门永久阻塞

    

      

        

文件

行

写死内容

        

          

governance/evidence_contract.py

216-219

release_path != "/opt/eimemory/releases/{commit}"

current_link != "/opt/eimemory/current"

health.url != "http://127.0.0.1:8091/health"

          

governance/code_automation_policy.py

596

同上 + 
observation_seconds != 172_800
（48 小时）

          

governance/deployment_receipt.py

25-27

DEFAULT_DEPLOYMENT_REPO_ROOT
 / 
CURRENT_LINK
 / 
HEALTH_URL

          

governance/promotion_manager.py

4017-4018

内联裸地址（含 
-c
 字符串形式）

          

governance/capability_dashboard.py

1045,1049,1065

releases_root == "/opt/eimemory/releases"

          

governance/safety/kill_switch.py

44

/var/lib/eimemory/state/audit.jsonl

        

      

      

evidence_contract.py:216-219
 是
部署回执的验证谓词
。用户安装路径若不符，
continue
 会跳过该回执 → 回执链断裂 → 晋级门永久阻塞，且
失败信息是静默的
（
continue
 而非报错），排查成本极高。

      

deployment_receipt.py
 已定义 
DEFAULT_*
 常量，说明作者
有意参数化
，但消费点直接写死字面量而非引用常量——参数化未落地。

    

  

  

    

P1

HC-08
环境变量默认值仍指向作者路径

    

      

eimemory/retrieval/vector_sync_worker.py:40

Runtime.create(root=os.environ.get(
'EIMEMORY_ROOT'
, 
'/var/lib/eimemory'
))

      
同类分布：
ops/openclaw_loop.py:82
、
adapters/hermes/channel_delivery.py:16,18
、
adapters/hermes/code_implementation.py:38
、
ops/code_implementation_owner.py:31,36,37
、
api/runtime.py:1728,1746,1747
、
governance/tool_receipts.py:152
 等约 20 处。

      
这类「看似可配置」的默认值最危险——用户未设置环境变量时不报错，而是静默指向 
/var/lib/eimemory
（需 root 权限）或 
/opt/eimemory
。

      

正例：

config/defaults.py:13
 的 
Path.home() / ".openclaw" / "memory" / "eimemory"
 才是正确的中性默认。

    

  

  

    

P1

HC-09

rollout_radius
 默认携带作者节点名

    

      

eimemory/governance/autonomy_policy.py:9,26

rollout_radius: str = 
"honxin_single_scope"

      
该字段被 
autonomy_controller.py:94
 输出到自主性策略快照。用户未配置时策略记录携带作者私有节点名，且该值
无枚举校验
，语义不明。

    

  

  

    

P1

HC-10
出厂默认作用域指向作者

    

      

        

文件

行

内容

        

          

evaluation/hongtu_catalog.py

25-30

RUNTIME_SCOPE = {"agent_id":"hongtu","workspace_id":"embodied","user_id":"darrow"}

          

ops/code_implementation_owner.py

40-45

PRODUCTION_RUNTIME_SCOPE
 同上

          

persona/schema.py

20

user_name: str = "darrow"

          

adapters/eibrain/rpc_server.py

138

_first_query_value(query, "user_id", "darrow")

          

cli/l1_worker.py

152

... or "darrow"

        

      

      
出厂默认 scope 与 persona 关系对象指向作者身份。用户未显式配置时，记忆会写入作者命名空间。

    

  

  

    

P1

HC-11
配置层被旁路（架构根因）

    

      

eimemory/config/
 提供了完整机制：

      

        

loader.py:21-27
 支持 
EIMEMORY_CONFIG_PATH
 或 
EIMEMORY_CONFIG_DIR/settings.json

        

schema.py
 定义 
root
 / 
default_agent_id
 / 
rpc_host
 / 
rpc_port

        

defaults.py:7-13
 提供中性默认根目录

      

      
但全项目
仅 6 处消费
：

      
eimemory/api/runtime.py:26          from eimemory.config.defaults import default_root
eimemory/cli/doctor.py:874,892,975  load_settings()
eimemory/cli/main.py:24,1351        load_settings()

      

这是 HC-01 / HC-03 / HC-07 / HC-08 的共同根因。
配置层只被 CLI 与 doctor 使用，治理平面、部署平面、适配器平面各自定义模块级常量。

典型表现：
Settings.rpc_port
 默认 8091 且可通过 
settings.json
 覆盖，但 
eimemory-rpc.service:26
 用命令行 
--port 8091
 直接覆盖配置——
配置文件与单元文件形成两套并行且互相冲突的真相来源
。

    

  

  

    

P1

HC-12
面向最终用户的桥接插件身份写死

    

      

integrations/openclaw/eimemory-bridge/index.js

:13  const DEFAULT_OPERATOR_USER_ID = 
'darrow'
;

:26  DEFAULT_REPLY_DELIVERY_STATE_PATH = 
'/var/lib/eimemory/openclaw_reply_delivery_state.json'
;

:27  DEFAULT_REPLY_DELIVERY_ATTEMPTS_PATH = 
'/var/lib/eimemory/...'
;

:28  DEFAULT_RELEASE_CLOSURE_SIGNAL_PATH = 
'/var/lib/eimemory/state/...'

      
该文件是
交付给用户的集成插件
（非内部代码），其身份与路径默认值直接影响终端用户。同目录 README 指导用户设置 
EIMEMORY_RPC_URL=http://honxin:8091/
——
honxin
 是作者主机名。

    

  

  
P2 · 一般级发现

  

    

P2

HC-13
文档与示例中的作者环境路径

    

      

        

docs/deployment.md:12,19,30,38,51,55,128,137,162-205
 — 全部部署指令指向 
/dev-project/eimemory
 与 
/home/darrow

        

examples/evaluation/*.json
 — 7 个示例数据集全部 
"user_id": "darrow"
（
production_recall_smoke.json
 有 16 处）

        

scripts/openclaw_loop.py:18-19
、
scripts/record_outcome_trace.py:18
、
scripts/convert_*.py

        

integrations/hermes/host-patches/memory-sync-snapshot.patch:157-168
 — 
/home/darrow/tmp/...

      

      

自我矛盾：

docs/deployment.md:104-105
 明确写着「Services must not depend on 
/home/&lt;user&gt;/dev-project
」——文档
已认识到该原则
，却在同文件的部署指令中全面违反。

    

  

  

    

P2

HC-14
作者身份污染公共命名空间

    

      

        

governance/capability_probe_executor.py:101,113
 — 路由 ID 
"deployment.honxin"

        

governance/capability_replay_packs.py:871
 — 
("route_deploy_via_tailscale", "Deploy honxin", "must use Tailscale/user systemd deploy path")

        

governance/capability_acceptance.py:279
 — 
{"intent": "deploy", "host": "honxin"}

        

identity.py:175
 — 
{"hongtu", "eibrain", "honxin", "main"}

      

      
作者私有节点名进入
能力路由 ID 与能力注册表
，成为对外可见公共命名空间的一部分。全项目 
hongtu|honjia|eibrain
 字面量共 49 处。

    

  

  

    

P2

HC-15
硬编码默认分支 
master

    

      
位点：
code_evolution_effects.py:49
、
code_evolution_bridge.py:53,243
、
cli/main.py:647
、
api/runtime.py:1800
、
code_automation_policy.py:557
。

      

code_automation_policy.py:557
 同时校验 
root != "/dev-project/eimemory"
、
remote != "origin"
、
branch != "master"
 三项。GitHub 默认分支已是 
main
，新用户克隆后默认分支不匹配，自演化策略校验直接失败。

    

  

  
正面结论 · 不应改动

  
以下设计为项目可移植性的
正确范式
，修复工作应以它们为标准。

  

    

零硬编码凭据
针对 
api_key|token|secret|password|credential
 后接 16+ 字符字面量的全量扫描
零命中
。凭据一律经环境变量注入。

    

LLM 接入层供应商中立

eimemory/llm/command_client.py
 采用 JSON stdin/stdout 命令协议（
CommandLLMClient
），通过 
EIMEMORY_LLM_COMMAND
 传入任意 argv 数组，对供应商零假设。这是整个项目最干净的可移植性设计。

    

retrieval/relevance.py
 是配置化样板

RelevanceConfig.from_env()
 将全部 9 个参数暴露为环境变量，默认端点强制 loopback，参数带完整边界校验。
建议作为治理平面重构的参照实现。

    

时区处理正确
未发现 
Asia/Shanghai
 / 
UTC+8
 硬编码；时间统一经 
astimezone(timezone.utc)
 归一化（20+ 处）。
core/clock.py:10-11
 的注释还记录了作者本地 
+08:00
 曾导致 L5 投影偏差的历史教训。

    

平台分支均有守卫
全部 
sys.platform
 / 
os.name
 判断（30+ 处）都用于
降级或跳过
，而非假设平台。
cli/doctor.py:522,588
 非 Linux 时返回 
SKIP
 并说明原因。

    

中性默认与占位范式已存在

config/defaults.py:13
 使用 
Path.home() / ".openclaw" / "memory" / "eimemory"
；
deploy/governance.env.example
 使用 
/home/USER/...
 占位符。

  

  
根因分析

  
三层结构性问题，逐层加深：

  
第一层 · 配置层未成为单一真相来源

  

eimemory/config/
 只被 CLI 消费（6 处），治理/部署/适配器平面各自持有模块级常量。
Settings.rpc_port
 与 
eimemory-rpc.service --port 8091
 并存即为例证。

  
第二层 · 配置通道半接通

  

EIMEMORY_AGENT_ID
 等变量被适配器平面读取并在安装时导出，但 
identity.py
 完全不读。这比「完全没做」更危险——安装脚本声称已参数化，运行时却按作者身份计算。

  
第三层 · 作者环境事实被固化为安全锚点

  
HC-01 的 
TRUSTED_*
 与 HC-07 的契约校验，本意是防止自演化平面被误用（
安全设计意图正确
），但实现方式把「作者的具体部署参数」等同于「安全边界」。
安全锚点应锚定「配置声明的部署身份」，而非字面量路径。

  
修复路线图

  
阶段一 · 解除阻断（P0）

  

    

identity.py
 接入四个 
EIMEMORY_*
 身份变量，默认值中性化；移除 
FEISHU_DARROW_OPEN_ID
；删除 
_hardware_node_from_record
 退化分支（HC-04/05）

    

TRUSTED_REPOSITORY_ROOT
 / 
TRUSTED_REMOTE
 / 
TRUSTED_BRANCH
 改为配置注入，
TRUSTED_BRANCH
 支持 
main
（HC-01/15）

    
移除 
100.105.189.120
，默认改 
127.0.0.1
；同步修正 
tests/test_deployment_tools.py:445
、
tests/test_platform.py:81
 的固化断言（HC-02）

    

deploy/systemd/*.service
 与 
deploy/*.sh
 中的 
darrow
 / 
/home/darrow
 改为占位或变量（HC-03）

  

  
阶段二 · 消除静默失效（P1）

  

    

ALLOWED_REVIEW_MODELS
 改为能力校验或配置驱动（HC-06）

    
契约校验改为引用 
deployment_receipt.py
 的 
DEFAULT_*
 常量并允许覆盖；
continue
 静默跳过改为可诊断的显式失败（HC-07）

    
全部 
os.environ.get(..., "/var/lib/...")
 默认值改为中性路径或强制必填（HC-08）

    
出厂默认 scope 与 persona 中性化（HC-09/10）

  

  
阶段三 · 架构收口（防复发）

  

    
建立约束：治理/部署平面禁止新增模块级路径常量，统一经 
eimemory/config/
 读取

    
新增回归测试：断言源码与部署目录中不出现作者身份字面量（允许列表仅限 
tests/fixtures
 与历史审计文档）

    
以 
retrieval/relevance.py
 为参照，为治理平面补齐配置化改造

  

  
审计基线 
6b5026a
 / v1.13.18　·　扫描口径 16 类正则，全部候选点经源码上下文逐行复核　·　15 个缺陷编号由 247 项实例归并而来

  
归档路径：
docs/audit/HARDCODE-PORTABILITY-AUDIT-2026-09-23.md

