from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
import json
import re

from eimemory.persona.correction import correction_from_user_text, persona_feedback_from_user_text


# Tencent L1 types: persona / episodic / instruction.
L1_ATOM_TYPES = frozenset({"persona", "episodic", "instruction"})
_USER_LINE = re.compile(r"(?:^|\n)User:\s*(.+?)(?=\nAssistant:|\Z)", re.DOTALL)
_ASSISTANT_LINE = re.compile(r"(?:^|\n)Assistant:\s*(.+?)\Z", re.DOTALL)
_ONE_SHOT = re.compile(r"(这次|本单|帮我翻译|帮我看看这个|hello|hi\b|你好[啊吗]?)$")
_INSTRUCTION_MARKERS = (
    "以后都",
    "以后",
    "从现在开始",
    "记住",
    "必须",
    "不要再",
    "别再",
    "先给结论",
    "要求你",
    "希望你以后",
    "用户要求",
)
_PERSONA_MARKERS = (
    "沟通风格",
    "偏好",
    "喜欢",
    "习惯",
    "讨厌废话",
    "我是",
    "我这个人",
    "饮食禁忌",
)
_EPISODIC_MARKERS = ("决定了", "已完成", "计划", "达成", "签约", "上线了")


@dataclass(frozen=True, slots=True)
class L1Atom:
    text: str
    title: str
    memory_type: str
    semantic_key: str
    category: str
    source_message_ids: tuple[str, ...] = ()


# Backward-compatible alias used by earlier turn-sediment callers.
DistilledFact = L1Atom


_L1_SYSTEM_PROMPT = """你是记忆提取器。只从用户新消息提取可长期复用的原子记忆。
类型仅限 persona、episodic、instruction。宁缺毋滥；一次性请求、闲聊、问候不要提取。
离开对话也要能看懂。返回 JSON 数组，每项: content, type, priority。
instruction=长期行为规则；persona=稳定偏好/身份；episodic=客观事件。priority 低于 70 的丢弃。"""


def extract_l1_atoms(
    *,
    user_text: str = "",
    assistant_text: str = "",
    turn_text: str = "",
    source_message_ids: tuple[str, ...] | list[str] | None = None,
    llm: object | None = None,
    use_llm: bool = False,
) -> list[L1Atom]:
    """Extract Tencent-style L1 atoms from an L0 turn. Chatter returns empty."""

    user, assistant = _split_turn(user_text=user_text, assistant_text=assistant_text, turn_text=turn_text)
    ids = tuple(str(item).strip() for item in (source_message_ids or ()) if str(item).strip())
    if not user or _is_chatter(user):
        return []
    if use_llm:
        client = llm
        if client is None:
            from eimemory.llm.hermes_adapter import resolve_l1_llm_client

            client = resolve_l1_llm_client()
        if client is not None:
            try:
                extracted = _extract_with_llm(client, user=user, assistant=assistant, source_message_ids=ids)
                if extracted is not None:
                    return extracted
            except Exception:
                pass
    atom_type = _classify_atom_type(user)
    if atom_type is None:
        return []
    feedback = persona_feedback_from_user_text(user)
    correction = feedback or correction_from_user_text(user)
    if atom_type == "instruction":
        text = user if len(user) >= 8 else str(correction.rule_candidate or user).strip()
    elif atom_type == "persona":
        text = user if "用户" in user or "鸿哥" in user else f"用户（鸿哥）{user}"
    else:
        text = user
    text = re.sub(r"\s+", " ", text).strip()
    if not text:
        return []
    title = text[:72]
    ids = tuple(str(item).strip() for item in (source_message_ids or ()) if str(item).strip())
    return [
        L1Atom(
            text=text,
            title=title,
            memory_type=atom_type,
            semantic_key=semantic_key(memory_type=atom_type, title=title),
            category=atom_type,
            source_message_ids=ids,
        )
    ]


def distill_turn(*, user_text: str = "", assistant_text: str = "", turn_text: str = "") -> L1Atom | None:
    atoms = extract_l1_atoms(user_text=user_text, assistant_text=assistant_text, turn_text=turn_text)
    return atoms[0] if atoms else None


def semantic_key(*, memory_type: str, title: str) -> str:
    normalized = re.sub(r"\s+", " ", f"{memory_type}:{title}".strip().lower())
    return "sk:" + sha256(normalized.encode("utf-8", errors="replace")).hexdigest()[:24]


def _extract_with_llm(client: object, *, user: str, assistant: str, source_message_ids: tuple[str, ...]) -> list[L1Atom] | None:
    complete = getattr(client, "complete", None)
    if not callable(complete):
        return None
    result = complete(
        system_prompt=_L1_SYSTEM_PROMPT,
        user_prompt=f"USER:\n{user}\n\nASSISTANT:\n{assistant}",
        json_mode=True,
    )
    text = str(getattr(result, "text", "") or "").strip()
    if not text:
        return None
    payload = json.loads(text)
    if isinstance(payload, dict):
        payload = payload.get("memories") or payload.get("items") or []
    if not isinstance(payload, list):
        return None
    atoms: list[L1Atom] = []
    for item in payload:
        if not isinstance(item, dict):
            continue
        content = re.sub(r"\s+", " ", str(item.get("content") or "").strip())
        atom_type = str(item.get("type") or "").strip().lower()
        try:
            priority = float(item.get("priority") or 0)
        except (TypeError, ValueError):
            priority = 0.0
        if not content or atom_type not in L1_ATOM_TYPES or priority < 70:
            continue
        title = content[:72]
        atoms.append(
            L1Atom(
                text=content,
                title=title,
                memory_type=atom_type,
                semantic_key=semantic_key(memory_type=atom_type, title=title),
                category=atom_type,
                source_message_ids=source_message_ids,
            )
        )
    return atoms


def _split_turn(*, user_text: str, assistant_text: str, turn_text: str) -> tuple[str, str]:
    user = str(user_text or "").strip()
    assistant = str(assistant_text or "").strip()
    blob = str(turn_text or "").strip()
    if not user and blob:
        match = _USER_LINE.search(blob)
        if match:
            user = match.group(1).strip()
    if not assistant and blob:
        match = _ASSISTANT_LINE.search(blob)
        if match:
            assistant = match.group(1).strip()
    return user, assistant


def _is_chatter(user: str) -> bool:
    compact = re.sub(r"\s+", " ", user).strip()
    if len(compact) < 6:
        return True
    lowered = compact.lower()
    if _ONE_SHOT.search(compact) or _ONE_SHOT.search(lowered):
        return True
    if compact in {"好的", "收到", "谢谢", "ok", "OK"}:
        return True
    return False


def _classify_atom_type(user: str) -> str | None:
    if any(marker in user for marker in _INSTRUCTION_MARKERS):
        return "instruction"
    if any(marker in user for marker in _PERSONA_MARKERS):
        return "persona"
    if any(marker in user for marker in _EPISODIC_MARKERS) and "这次" not in user:
        return "episodic"
    feedback = persona_feedback_from_user_text(user)
    if feedback is None:
        return None
    if feedback.category in {"verbosity", "tone", "latency", "memory"}:
        return "instruction"
    if feedback.category in {"correctness", "resourcefulness"}:
        return "instruction"
    return "persona"
