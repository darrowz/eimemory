from __future__ import annotations

import json
from typing import Any

from eimemory.persona.context_router import route_persona_context
from eimemory.persona.correction import correction_from_user_text
from eimemory.persona.evals.run_persona_eval import run_persona_eval
from eimemory.persona.evolver import evolve_persona
from eimemory.persona.prompt import build_persona_guidance
from eimemory.persona.store import PersonaStore


def add_persona_parser(sub: Any) -> None:
    persona = sub.add_parser("persona")
    persona_sub = persona.add_subparsers(dest="persona_command")
    persona_sub.add_parser("show")
    route = persona_sub.add_parser("route")
    route.add_argument("--text", required=True)
    guidance = persona_sub.add_parser("guidance")
    guidance.add_argument("--text", required=True)
    guidance.add_argument("--max-chars", type=int, default=800)
    correct = persona_sub.add_parser("correct")
    correct.add_argument("--text", required=True)
    evolve = persona_sub.add_parser("evolve")
    evolve.add_argument("--dry-run", action="store_true")
    persona_sub.add_parser("eval")


def handle_persona_command(parsed: Any, runtime: Any, scope: dict[str, Any]) -> int:
    store = PersonaStore(runtime.store)
    state = store.load_state()
    command = str(getattr(parsed, "persona_command", "") or "")
    if command == "show":
        print(json.dumps(state.to_dict(), ensure_ascii=False, indent=2))
        return 0
    if command == "route":
        route = route_persona_context(str(parsed.text or ""), state=state)
        print(json.dumps(route.to_dict(), ensure_ascii=False, indent=2))
        return 0
    if command == "guidance":
        guidance = build_persona_guidance(text=str(parsed.text or ""), state=state, max_chars=int(parsed.max_chars or 800))
        print(json.dumps(guidance.to_dict(), ensure_ascii=False, indent=2))
        return 0
    if command == "correct":
        correction = correction_from_user_text(str(parsed.text or ""))
        store.record_correction(correction, scope=scope)
        print(json.dumps(correction.to_dict(), ensure_ascii=False, indent=2))
        return 0
    if command == "evolve":
        corrections = store.list_corrections(scope=scope, limit=100)
        result = evolve_persona(state, corrections, store=store, scope=scope, dry_run=bool(parsed.dry_run))
        print(json.dumps(result.to_dict(), ensure_ascii=False, indent=2))
        return 0
    if command == "eval":
        report = run_persona_eval()
        store.record_eval_result(report, scope=scope)
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0
    print(json.dumps({"ok": False, "error": "usage", "usage": "eimemory persona show|route|guidance|correct|evolve|eval"}))
    return 2
