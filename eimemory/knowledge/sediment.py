from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
import json
import re

from eimemory.knowledge.l1_prompts import EXTRACT_MEMORIES_SYSTEM_PROMPT
from eimemory.persona.correction import correction_from_user_text, persona_feedback_from_user_text


# Tencent L1 types: persona / episodic / instruction.
L1_ATOM_TYPES = frozenset({"persona", "episodic", "instruction"})
_USER_LINE = re.compile(r"(?:^|\n)User:\s*(.+?)(?=\nAssistant:|\Z)", re.DOTALL)
_ASSISTANT_LINE = re.compile(r"(?:^|\n)Assistant:\s*(.+?)\Z", re.DOTALL)
_ONE_SHOT = re.compile(
    r"(这次|本单|帮我翻译|帮我看看这个|帮我查|看一下|看下github|hello|hi\b|你好[啊吗]?)$"
)
_QUESTION = re.compile(
    r"(吗|呢|怎么样|如何|行不行|可不可以|什么时候|为什么|哪[个里]|是否|合适)\s*[?？]?\s*$"
)
_SECRET = re.compile(
    r"(ssh-ed25519|ssh-rsa|BEGIN OPENSSH|BEGIN PRIVATE KEY|password\s*=|token\s*=|vless://|api[_-]?key)",
    re.I,
)
_CRON_WRAP = re.compile(r"\[IMPORTANT: You are running as a scheduled cron", re.I)
_INSTRUCTION_MARKERS = (
    "以后都",
    "以后回答",
    "以后不要",
    "以后别",
    "从现在开始",
    "不要再",
    "别再",
    "先给结论",
    "希望你以后",
)
_PERSONA_MARKERS = (
    "沟通风格",
    "讨厌废话",
    "我这个人",
    "饮食禁忌",
    "我是学",
)
_EPISODIC_MARKERS = ("决定了", "已完成", "签约", "上线了")


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


def extract_l1_atoms(
    *,
    user_text: str = "",
    assistant_text: str = "",
    turn_text: str = "",
    source_message_ids: tuple[str, ...] | list[str] | None = None,
    llm: object | None = None,
    use_llm: bool = False,
    fallback_heuristic: bool = True,
) -> list[L1Atom]:
    """Extract Tencent-style L1 atoms from an L0 turn. Chatter returns empty."""

    user, assistant = _split_turn(user_text=user_text, assistant_text=assistant_text, turn_text=turn_text)
    ids = tuple(str(item).strip() for item in (source_message_ids or ()) if str(item).strip())
    if not user or _reject_extract(user):
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
                extracted = None
        if not fallback_heuristic:
            return []
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
        system_prompt=EXTRACT_MEMORIES_SYSTEM_PROMPT,
        user_prompt=(
            "【上一个情境】无\n"
            f"【背景消息】无\n【待提取的新消息】\nUSER:\n{user}\n\nASSISTANT:\n{assistant}"
        ),
        json_mode=True,
    )
    text = str(getattr(result, "text", "") or "").strip()
    if not text:
        return None
    payload = json.loads(text)
    memories: list[dict] = []
    if isinstance(payload, dict):
        payload = payload.get("memories") or payload.get("items") or payload.get("scenes") or []
    if isinstance(payload, list):
        for item in payload:
            if not isinstance(item, dict):
                continue
            if isinstance(item.get("memories"), list):
                memories.extend(entry for entry in item["memories"] if isinstance(entry, dict))
            elif item.get("content"):
                memories.append(item)
    atoms: list[L1Atom] = []
    for item in memories:
        content = re.sub(r"\s+", " ", str(item.get("content") or "").strip())
        atom_type = str(item.get("type") or "").strip().lower()
        try:
            priority = float(item.get("priority") or 0)
        except (TypeError, ValueError):
            priority = 0.0
        min_priority = {"instruction": 70, "persona": 50, "episodic": 60}.get(atom_type, 70)
        if not content or atom_type not in L1_ATOM_TYPES or priority < min_priority:
            continue
        if _reject_extract(content):
            continue
        title = content[:72]
        source_ids = tuple(str(value) for value in (item.get("source_message_ids") or source_message_ids) if str(value).strip()) or source_message_ids
        atoms.append(
            L1Atom(
                text=content,
                title=title,
                memory_type=atom_type,
                semantic_key=semantic_key(memory_type=atom_type, title=title),
                category=atom_type,
                source_message_ids=source_ids,
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


def _reject_extract(user: str) -> bool:
    compact = re.sub(r"\s+", " ", user).strip()
    if len(compact) < 6 or len(compact) > 280:
        return True
    if _CRON_WRAP.search(compact) or _SECRET.search(compact):
        return True
    if compact in {"好的", "收到", "谢谢", "ok", "OK"}:
        return True
    lowered = compact.lower()
    if _ONE_SHOT.search(compact) or _ONE_SHOT.search(lowered):
        return True
    if _QUESTION.search(compact):
        return True
    if compact.endswith("?") or compact.endswith("？"):
        return True
    if compact.startswith(("查", "查询", "新查询")):
        return True
    if "看下" in compact or "帮我看" in compact:
        return True
    return False


def _is_chatter(user: str) -> bool:
    return _reject_extract(user)


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
