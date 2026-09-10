"""Narrow task-state/history intent and evidence shape, not an audit permission."""
from __future__ import annotations

import re

from eimemory.metadata import business_metadata
from .loadout import PERSONA_TYPES


_TASK = re.compile(r'任务|待办|\b(?:tasks?|todos?|work items?)\b', re.I)
_STATE_QUERY = re.compile(r'进展|进度|状态|做到哪|待验收|做完|完成了|哪些.{0,8}完成|'
                          r'\b(?:status|progress|pending|completed|finished|acceptance)\b', re.I)
_HISTORY_QUERY = re.compile(r'(?:上次|之前|当时|最近).{0,30}(?:授权|安排|约定|决定|任务)|'
                            r'(?:任务|待办).{0,12}历史|历史.{0,12}(?:任务|待办)|'
                            r'\bwhat did (?:we|i|you) (?:agree|authorize|decide|plan)\b|'
                            r'\b(?:tasks?.{0,30}history|history.{0,30}tasks?)\b|'
                            r'\b(?:previous|last|recent).{0,30}(?:tasks?|authorized|agreed)\b', re.I)
_RESEARCH_TOPIC = re.compile(r'(?:管理|调度|跟踪).{0,8}(?:软件|算法|论文|方法)|'
                            r'\b(?:task|progress).{0,25}(?:algorithm|software|paper)\b', re.I)
_STATE_FACT = re.compile(r'已完成|已交付|已提交|已部署|已通过|测试通过|待验收|等待验收|'
                         r'进行中|正在|尚未完成|未完成|已取消|被阻塞|已阻塞|进展[:：]|进度[:：]|'
                         r'\b(?:completed|finished|delivered|submitted|in progress|pending|blocked|cancelled)\b', re.I)
_HISTORY_FACT = re.compile(r'已授权|授权了|已安排|安排了|约定了|决定了|已决定|我们约定|'
                           r'\b(?:authorized|agreed|decided|assigned)\b', re.I)


def task_recall_mode(query: str) -> str:
    text = str(query or '')[:16000]
    if _RESEARCH_TOPIC.search(text):
        return ''
    if _TASK.search(text) and _STATE_QUERY.search(text):
        return 'status'
    if _HISTORY_QUERY.search(text):
        return 'history'
    return ''


def supports_task_evidence(mode: str, text: str) -> bool:
    if not mode:
        return True
    # Evaluate assertions separately from questions, proposed acceptance criteria
    # and commentary about retrieval. A version mention cannot establish state.
    if re.search(r'完成标准|验收标准', text) and not re.search(r'已部署|部署已完成|已交付|已提交|已授权|已安排', text):
        return False
    sentences = re.split(r'[。\n]', text)
    return any(
        not re.search(r'^(?:\w+\s+)?completed turn$|[？?]|完成标准|验收标准|查询.{0,12}(?:没答准|返回|命中)|'
                      r'(?:L0|L1).{0,12}(?:返回|找到|选中)|'
                      r'应该|需要.{0,8}(?:选出|返回)|\b(?:should|must return)\b', sentence, re.I)
        and bool(_STATE_FACT.search(sentence) or (mode == 'history' and _HISTORY_FACT.search(sentence)))
        for sentence in sentences)


def is_task_evidence(record, mode: str) -> bool:
    """Require task evidence, never a preference/rule masquerading as progress.

    Scope, source, freshness and pollution checks remain the caller's job.
    """
    if not mode or record.kind != 'memory':
        return False
    content = record.content if isinstance(record.content, dict) else {}
    meta = business_metadata(record.meta)
    memory_type = str(meta.get('memory_type') or content.get('memory_type') or '').lower()
    if memory_type in PERSONA_TYPES or memory_type in {'rule', 'system_rule', 'policy',
                       'audit', 'audit_record', 'run_log', 'runtime_log', 'incident_report', 'raw_chunk', 'raw'}:
        return False
    # Preserve evidence semantics while avoiding repeated full-transcript scans
    # once a short title/summary already establishes the requested state.
    return any(supports_task_evidence(mode, str(value or '')) for value in
               (record.title, record.summary, record.detail, content.get('text')))


def task_project_scope(query: str, context: dict) -> tuple[str, str]:
    """Resolve scope from the request/host, never from whichever candidate wins."""
    from eimemory.retrieval.answer_requirements import explicit_project
    if re.search(r'全局|全部|所有|跨项目|\b(?:all|global|across projects)\b', query, re.I):
        return 'global', ''
    project = explicit_project(query)
    named = re.match(r'([A-Za-z][\w-]*)\s+(?:最近|最新|上次|任务|latest|recent|last)', query, re.I)
    if not project and named and named.group(1).lower() not in {'my', 'our', 'the', 'what', 'all'}:
        project = named.group(1)
    if not project:
        project = str(context.get('project') or context.get('project_name') or context.get('project_id') or '').strip()
    if project:
        return 'project', project[:256]
    # An explicitly named task is already narrower than project scope.
    subject = re.search(r'([\u4e00-\u9fffA-Za-z][\w-]*?)任务', query)
    if subject and not re.fullmatch(r'(?:最近|最新|上次|之前|已授权|的|我|我们|查看|历史|授权|了|什么|哪些)+', subject.group(1)):
        return 'task', subject.group(1)
    return 'ambiguous', ''
