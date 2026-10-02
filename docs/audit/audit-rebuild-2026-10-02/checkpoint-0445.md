# 从零四线审计检查点 · 2026-10-02 04:45 UTC

固定全项目分母7,140函数：第一遍705（9.87%），独立双遍434（6.08%），271个第一遍函数在第二遍队列中。新增完整storage repository首审125函数、LLM可审范围93/94、models53；LLM启动helper41–67保持未审。

双遍已完成storage163、retrieval256、core10、embeddings5；新storage125与并行LLM/models尚不算双遍。每个函数连接原始图ID、源SHA与其阅读者的封存artifact，单遍不会被配对脚本自动当作第二遍。

补正物理行元数据说明：图file节点本身没有行数，但index.json.files数组和伴随files.json均有line_count，两者逐项相同。固定622源码文件合207,866物理行；未建立完整的已审行分子，所以仍不报告行覆盖百分比，不把nested定义行数相加。此前函数/文件分母与比例不变。

已有11个修复批次获独立复核并远端读回（001–007、009–012）；008仍hold。83是各独立检查点累计不同fake/AST用例，不能称combined83全通过；未做真实数据库/provider执行。

本次新增源码候选：013 gateway失败worker仍占池容量；014主动检索worker启动/交接；015 one-shot stdin跨chunk UTF-8解码。13/15来自并行首审，14来自独立双遍。两个storage事务/内存队列候选仍等待盲审确认。禁止执行的领域操作没有因为扩大静态源读而开放。
