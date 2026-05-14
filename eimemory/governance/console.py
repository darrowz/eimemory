from __future__ import annotations

import html
import json
from pathlib import Path
from typing import Any


def render_evolution_console(snapshot: dict) -> str:
    snapshot_payload = json.dumps(snapshot, ensure_ascii=False, sort_keys=True, indent=2)
    snapshot_json = html.escape(snapshot_payload)

    scope = _normalize_mapping(snapshot.get("scope"))
    memory_quality = _normalize_mapping(snapshot.get("memory_quality"))
    rules = _normalize_mapping(snapshot.get("rules"))
    recall_gaps = _normalize_mapping(snapshot.get("recall_gaps"))
    source_candidates = _normalize_mapping(snapshot.get("source_candidates"))
    active_intake = _normalize_mapping(snapshot.get("active_intake"))
    backups = _normalize_mapping(snapshot.get("backups"))
    health = _normalize_mapping(snapshot.get("health"))
    reflection_stats = _normalize_mapping(snapshot.get("reflection_stats"))

    workspace = scope.get("workspace_id") or "global"
    agent = scope.get("agent_id") or "unknown-agent"
    tenant = scope.get("tenant_id") or "default"
    health_ok = bool(health.get("ok"))
    status = "Healthy" if health_ok else "Needs attention"
    warnings = health.get("warnings") or []
    generated_at = snapshot.get("generated_at") or "unknown"
    schema_version = snapshot.get("snapshot_schema_version") or "unknown"

    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>eimemory Evolution Console - {_escape_text(workspace)}</title>
  <style>
    :root {{
      color-scheme: dark;
      --bg: #08090a;
      --panel: #111214;
      --panel-2: #15171a;
      --line: #2c3035;
      --line-soft: rgba(255,255,255,0.07);
      --text: #f3f5f7;
      --body: #b9c0c8;
      --muted: #7f8894;
      --green: #21d08f;
      --yellow: #f3b13c;
      --red: #ff6267;
      --code: SFMono-Regular, Menlo, Monaco, Consolas, "Liberation Mono", monospace;
      --sans: Inter, system-ui, -apple-system, "Segoe UI", Roboto, Arial, sans-serif;
    }}
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0;
      min-height: 100vh;
      background: radial-gradient(circle at top left, rgba(33, 208, 143, 0.10), transparent 360px), var(--bg);
      color: var(--text);
      font-family: var(--sans);
    }}
    main {{
      width: min(1320px, calc(100% - 28px));
      margin: 0 auto;
      padding: 14px 0 32px;
    }}
    .topbar {{
      display: grid;
      grid-template-columns: minmax(240px, 1fr) auto auto;
      align-items: center;
      gap: 12px;
      min-height: 50px;
      margin-bottom: 12px;
      padding: 10px 12px;
      border: 1px solid var(--line);
      border-radius: 10px;
      background: rgba(17, 18, 20, 0.88);
    }}
    .title {{
      display: flex;
      align-items: center;
      gap: 10px;
      min-width: 0;
    }}
    .mark {{
      width: 10px;
      height: 18px;
      flex: 0 0 auto;
      clip-path: polygon(44% 0, 100% 0, 58% 42%, 100% 42%, 18% 100%, 38% 55%, 0 55%);
      background: var(--green);
      filter: drop-shadow(0 0 7px rgba(33, 208, 143, 0.7));
    }}
    h1 {{
      margin: 0;
      font-size: 18px;
      line-height: 1.1;
      letter-spacing: -0.02em;
    }}
    .scope {{
      min-width: 0;
      color: var(--muted);
      font-family: var(--code);
      font-size: 11px;
      overflow: hidden;
      text-overflow: ellipsis;
      white-space: nowrap;
    }}
    .snapshot-meta {{
      margin-top: 3px;
      color: var(--muted);
      font-family: var(--code);
      font-size: 10px;
      overflow: hidden;
      text-overflow: ellipsis;
      white-space: nowrap;
    }}
    .pill {{
      display: inline-flex;
      align-items: center;
      gap: 7px;
      padding: 6px 10px;
      border-radius: 999px;
      border: 1px solid {_escape_text('rgba(33, 208, 143, 0.45)' if health_ok else 'rgba(243, 177, 60, 0.55)')};
      color: {_escape_text('#21d08f' if health_ok else '#f3b13c')};
      background: {_escape_text('rgba(33, 208, 143, 0.08)' if health_ok else 'rgba(243, 177, 60, 0.10)')};
      font-size: 12px;
      font-weight: 700;
      white-space: nowrap;
    }}
    .pill::before {{
      content: "";
      width: 7px;
      height: 7px;
      border-radius: 999px;
      background: currentColor;
    }}
    .warnings {{
      color: var(--muted);
      font-size: 12px;
      text-align: right;
      white-space: nowrap;
      overflow: hidden;
      text-overflow: ellipsis;
      max-width: 360px;
    }}
    .kpis {{
      display: grid;
      grid-template-columns: repeat(6, minmax(0, 1fr));
      gap: 8px;
      margin-bottom: 10px;
    }}
    .kpi, section {{
      border: 1px solid var(--line);
      border-radius: 10px;
      background: linear-gradient(180deg, rgba(21, 23, 26, 0.96), rgba(14, 15, 17, 0.96));
    }}
    .kpi {{
      padding: 10px 12px;
      min-height: 64px;
    }}
    .label {{
      display: block;
      margin-bottom: 5px;
      color: var(--muted);
      font-family: var(--code);
      font-size: 10px;
      text-transform: uppercase;
      letter-spacing: 0.04em;
    }}
    .value {{
      display: block;
      color: var(--text);
      font-size: 24px;
      line-height: 1;
      font-weight: 700;
      letter-spacing: -0.03em;
    }}
    .grid {{
      display: grid;
      grid-template-columns: 1.1fr 0.9fr 0.9fr;
      gap: 10px;
      align-items: start;
    }}
    .span-2 {{ grid-column: span 2; }}
    .span-3 {{ grid-column: 1 / -1; }}
    section {{
      padding: 13px;
      overflow: hidden;
    }}
    section h2 {{
      margin: 0 0 9px;
      font-size: 15px;
      line-height: 1.15;
      letter-spacing: -0.01em;
    }}
    .sub {{
      margin: -3px 0 10px;
      color: var(--muted);
      font-size: 12px;
      line-height: 1.35;
    }}
    .mini-grid {{
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(96px, 1fr));
      gap: 7px;
    }}
    .mini {{
      padding: 8px 9px;
      border: 1px solid var(--line-soft);
      border-radius: 8px;
      background: rgba(255,255,255,0.025);
    }}
    .mini .label {{ margin-bottom: 4px; }}
    .mini .value {{ font-size: 19px; }}
    .bars {{
      display: grid;
      gap: 7px;
      margin-top: 10px;
    }}
    .bar-row {{
      display: grid;
      grid-template-columns: 82px 1fr 34px;
      align-items: center;
      gap: 8px;
      color: var(--body);
      font-size: 12px;
    }}
    .bar-track {{
      height: 7px;
      border-radius: 999px;
      overflow: hidden;
      background: #07080a;
      border: 1px solid var(--line-soft);
    }}
    .bar-fill {{
      width: var(--width);
      height: 100%;
      background: linear-gradient(90deg, var(--green), rgba(33, 208, 143, 0.35));
    }}
    .kv {{
      display: grid;
      gap: 0;
      margin-top: 9px;
      border-top: 1px solid var(--line-soft);
    }}
    .kv-title {{
      margin: 8px 0 2px;
      color: var(--muted);
      font-family: var(--code);
      font-size: 10px;
      text-transform: uppercase;
    }}
    .kv-row {{
      display: flex;
      justify-content: space-between;
      gap: 10px;
      padding: 5px 0;
      border-bottom: 1px solid var(--line-soft);
      color: var(--body);
      font-size: 12px;
    }}
    .kv-row strong {{
      color: var(--text);
      font-weight: 600;
    }}
    .records {{
      display: grid;
      gap: 7px;
      margin-top: 8px;
    }}
    .record {{
      padding: 9px;
      border: 1px solid var(--line-soft);
      border-radius: 8px;
      background: rgba(5, 6, 7, 0.42);
    }}
    .record-title {{
      color: var(--text);
      font-size: 13px;
      line-height: 1.28;
      font-weight: 700;
      margin-bottom: 4px;
    }}
    .record-summary {{
      color: var(--body);
      font-size: 12px;
      line-height: 1.4;
      white-space: pre-wrap;
    }}
    .meta {{
      margin-top: 5px;
      color: var(--muted);
      font-family: var(--code);
      font-size: 10px;
      line-height: 1.35;
      white-space: pre-wrap;
      word-break: break-word;
      max-height: 70px;
      overflow: auto;
    }}
    .muted {{
      color: var(--muted);
      font-size: 12px;
      line-height: 1.4;
    }}
    details {{
      border: 1px solid var(--line);
      border-radius: 10px;
      background: rgba(14, 15, 17, 0.96);
    }}
    summary {{
      cursor: pointer;
      padding: 11px 13px;
      color: var(--body);
      font-size: 13px;
      font-weight: 700;
    }}
    pre {{
      margin: 0;
      max-height: 360px;
      padding: 12px;
      border-top: 1px solid var(--line);
      color: #d9f7ea;
      background: #050607;
      overflow: auto;
      white-space: pre-wrap;
      word-break: break-word;
      font-family: var(--code);
      font-size: 11px;
      line-height: 1.4;
    }}
    @media (max-width: 1100px) {{
      .topbar {{ grid-template-columns: 1fr auto; }}
      .warnings {{ grid-column: 1 / -1; text-align: left; max-width: none; }}
      .kpis {{ grid-template-columns: repeat(3, minmax(0, 1fr)); }}
      .grid {{ grid-template-columns: 1fr 1fr; }}
      .span-2, .span-3 {{ grid-column: 1 / -1; }}
    }}
    @media (max-width: 700px) {{
      main {{ width: min(100% - 18px, 1320px); padding-top: 9px; }}
      .topbar {{ grid-template-columns: 1fr; align-items: start; }}
      .scope {{ white-space: normal; }}
      .kpis, .grid {{ grid-template-columns: 1fr; }}
      .span-2, .span-3 {{ grid-column: auto; }}
    }}
    /* Compact operations layout override. Keep the console readable first. */
    body {{
      background:
        linear-gradient(180deg, rgba(255,255,255,0.025), transparent 160px),
        radial-gradient(circle at 8% 0%, rgba(33, 208, 143, 0.08), transparent 280px),
        #08090a;
    }}
    main {{
      width: min(1180px, calc(100% - 24px));
      padding: 10px 0 24px;
    }}
    .topbar {{
      position: sticky;
      top: 0;
      z-index: 5;
      grid-template-columns: minmax(260px, 1fr) auto;
      min-height: 42px;
      margin-bottom: 8px;
      padding: 8px 10px;
      border-radius: 8px;
      backdrop-filter: blur(10px);
    }}
    .mark {{ display: none; }}
    h1 {{ font-size: 16px; }}
    .scope {{ font-size: 10px; }}
    .warnings {{
      grid-column: 1 / -1;
      max-width: none;
      text-align: left;
      font-size: 11px;
    }}
    .pill {{
      padding: 5px 9px;
      font-size: 11px;
    }}
    .kpis {{
      grid-template-columns: repeat(8, minmax(0, 1fr));
      gap: 6px;
      margin-bottom: 8px;
    }}
    .kpi {{
      min-height: 52px;
      padding: 8px 9px;
      border-radius: 8px;
    }}
    .label {{
      margin-bottom: 4px;
      font-size: 9px;
      letter-spacing: 0.03em;
    }}
    .value {{ font-size: 20px; }}
    .grid {{
      grid-template-columns: minmax(0, 1.35fr) minmax(320px, 0.65fr);
      gap: 8px;
    }}
    .span-2, .span-3 {{ grid-column: 1 / -1; }}
    section {{
      padding: 10px;
      border-radius: 8px;
    }}
    section h2 {{
      margin-bottom: 7px;
      font-size: 13px;
    }}
    .sub {{
      margin: -2px 0 8px;
      font-size: 11px;
    }}
    .mini-grid {{
      grid-template-columns: repeat(auto-fit, minmax(82px, 1fr));
      gap: 5px;
    }}
    .mini {{
      padding: 6px 7px;
      border-radius: 7px;
    }}
    .mini .value {{ font-size: 16px; }}
    .bars {{ gap: 5px; margin-top: 8px; }}
    .bar-row {{
      grid-template-columns: 74px 1fr 30px;
      font-size: 11px;
    }}
    .kv {{
      margin-top: 7px;
    }}
    .kv-title {{
      margin: 7px 0 2px;
      font-size: 9px;
    }}
    .kv-row {{
      padding: 4px 0;
      font-size: 11px;
    }}
    .records {{
      gap: 5px;
      margin-top: 6px;
    }}
    .record {{
      padding: 7px;
      border-radius: 7px;
    }}
    .record-title {{
      font-size: 12px;
      margin-bottom: 3px;
    }}
    .record-summary {{
      font-size: 11px;
      line-height: 1.34;
    }}
    .meta {{
      max-height: 48px;
      margin-top: 4px;
      font-size: 9px;
    }}
    .muted {{ font-size: 11px; }}
    details {{ border-radius: 8px; }}
    summary {{
      padding: 9px 10px;
      font-size: 12px;
    }}
    pre {{
      max-height: 280px;
      padding: 10px;
      font-size: 10px;
    }}
    @media (max-width: 980px) {{
      .kpis {{ grid-template-columns: repeat(4, minmax(0, 1fr)); }}
      .grid {{ grid-template-columns: 1fr; }}
    }}
    @media (max-width: 620px) {{
      main {{ width: min(100% - 14px, 1180px); }}
      .topbar {{ grid-template-columns: 1fr; position: static; }}
      .kpis {{ grid-template-columns: repeat(2, minmax(0, 1fr)); }}
    }}
    /* Fill mode: dense 12-column board with draggable cards. */
    main {{
      width: min(1440px, calc(100% - 20px));
    }}
    .topbar {{
      grid-template-columns: minmax(260px, 1fr) auto auto;
    }}
    .reset-layout {{
      appearance: none;
      border: 1px solid var(--line);
      border-radius: 999px;
      padding: 5px 9px;
      color: var(--body);
      background: rgba(255,255,255,0.035);
      font: 700 11px/1 var(--sans);
      cursor: pointer;
      white-space: nowrap;
    }}
    .reset-layout:hover {{
      border-color: rgba(33, 208, 143, 0.5);
      color: var(--green);
    }}
    .grid {{
      grid-template-columns: repeat(12, minmax(0, 1fr));
      grid-auto-flow: dense;
      align-items: stretch;
      gap: 8px;
    }}
    .grid > section {{
      grid-column: span 3;
      min-height: 148px;
      display: flex;
      flex-direction: column;
      transition: border-color .16s ease, transform .16s ease, opacity .16s ease;
    }}
    .grid > section.card-wide {{ grid-column: span 6; }}
    .grid > section.card-large {{ grid-column: span 7; }}
    .grid > section.card-medium {{ grid-column: span 4; }}
    .grid > section.card-tall {{ min-height: 210px; }}
    .grid > .span-3 {{ grid-column: 1 / -1; }}
    .grid > section[draggable="true"] h2::after {{
      content: "drag";
      float: right;
      margin-left: 8px;
      padding: 3px 6px;
      border: 1px solid var(--line-soft);
      border-radius: 999px;
      color: var(--muted);
      font: 700 9px/1 var(--code);
      letter-spacing: 0.04em;
      text-transform: uppercase;
    }}
    .grid > section.dragging {{
      opacity: .58;
      transform: scale(.99);
    }}
    .grid > section.drag-over {{
      border-color: rgba(33, 208, 143, 0.8);
      box-shadow: inset 0 0 0 1px rgba(33, 208, 143, 0.28);
    }}
    .records {{
      align-content: start;
    }}
    .card-large .records,
    .card-wide .records {{
      grid-template-columns: repeat(2, minmax(0, 1fr));
    }}
    .card-large .kv-title,
    .card-wide .kv-title {{
      grid-column: 1 / -1;
    }}
    @media (max-width: 1180px) {{
      .grid {{ grid-template-columns: repeat(8, minmax(0, 1fr)); }}
      .grid > section {{ grid-column: span 4; }}
      .grid > section.card-wide,
      .grid > section.card-large {{ grid-column: 1 / -1; }}
      .grid > section.card-medium {{ grid-column: span 4; }}
    }}
    @media (max-width: 720px) {{
      .topbar {{ grid-template-columns: 1fr auto; }}
      .warnings {{ grid-column: 1 / -1; }}
      .grid {{ grid-template-columns: 1fr; }}
      .grid > section,
      .grid > section.card-wide,
      .grid > section.card-large,
      .grid > section.card-medium {{
        grid-column: 1 / -1;
      }}
      .card-large .records,
      .card-wide .records {{
        grid-template-columns: 1fr;
      }}
    }}
  </style>
</head>
<body>
  <main>
    <header class="topbar">
      <div class="title">
        <span class="mark"></span>
        <div>
          <h1>Evolution Console</h1>
          <div class="scope">tenant={_escape_text(tenant)} / agent={_escape_text(agent)} / workspace={_escape_text(workspace)}</div>
          <div class="snapshot-meta">Generated {_escape_text(generated_at)} / Schema v{_escape_text(schema_version)}</div>
        </div>
      </div>
      <span class="pill">{_escape_text(status)}</span>
      <button class="reset-layout" type="button" data-reset-layout>Reset layout</button>
      <div class="warnings">{_render_warnings(warnings)}</div>
    </header>

    <div class="kpis">
      {_render_kpi("memories", memory_quality.get("memory_count"))}
      {_render_kpi("accepted", memory_quality.get("accepted_count"))}
      {_render_kpi("salience", memory_quality.get("average_salience"))}
      {_render_kpi("candidates", active_intake.get("candidate_count"))}
      {_render_kpi("papers", active_intake.get("paper_source_count"))}
      {_render_kpi("pages", active_intake.get("knowledge_page_count"))}
      {_render_kpi("rules", rules.get("total_count"))}
      {_render_kpi("gaps", recall_gaps.get("unknown_count"))}
    </div>

    <div class="grid">
      <section class="card card-wide" draggable="true" data-card-id="active-intake">
        <h2>Active Intake</h2>
        <div class="sub">Collection, paper promotion, and operational projection in one compact view.</div>
        <div class="mini-grid">
          {_render_mini("open", active_intake.get("open_candidate_count"))}
          {_render_mini("promoted", active_intake.get("promoted_candidate_count"))}
          {_render_mini("quarantined", active_intake.get("quarantined_candidate_count"))}
          {_render_mini("reviewed", active_intake.get("reviewed_candidate_count"))}
          {_render_mini("paper sources", active_intake.get("paper_source_count"))}
          {_render_mini("knowledge pages", active_intake.get("knowledge_page_count"))}
        </div>
      </section>

      <section class="card card-wide" draggable="true" data-card-id="memory-quality">
        <h2>Memory Quality</h2>
        <div class="mini-grid">
          {_render_mini("confirmed", _nested(memory_quality, "quality_distribution", "confirmed"))}
          {_render_mini("core", _nested(memory_quality, "quality_distribution", "core"))}
          {_render_mini("candidate", _nested(memory_quality, "quality_distribution", "candidate"))}
          {_render_mini("rejected", memory_quality.get("rejected_count"))}
          {_render_mini("missing", memory_quality.get("missing_quality_count"))}
        </div>
        {_render_quality_bars(memory_quality.get("quality_distribution"))}
      </section>

      <section class="card" draggable="true" data-card-id="rules">
        <h2>Rules</h2>
        <div class="mini-grid">
          {_render_mini("active", rules.get("active_count"))}
          {_render_mini("accepted", rules.get("accepted_count"))}
          {_render_mini("candidate", rules.get("candidate_count"))}
          {_render_mini("rejected", rules.get("rejected_count"))}
        </div>
      </section>

      <section class="card card-large card-tall" draggable="true" data-card-id="recent-papers">
        <h2>Recent Papers / Candidates</h2>
        <div class="sub">Most recent active learning artifacts across collection and compilation.</div>
        {_render_recent_papers_candidates(active_intake)}
      </section>

      <section class="card card-medium" draggable="true" data-card-id="external-intake">
        <h2>External Intake</h2>
        <div class="sub">Latest collection signal plus current candidate queue.</div>
        {_render_external_intake(active_intake)}
      </section>

      <section class="card card-medium" draggable="true" data-card-id="paper-promotion">
        <h2>Paper Promotion</h2>
        <div class="sub">Paper candidates promoted into structured knowledge.</div>
        {_render_paper_promotion(active_intake)}
      </section>

      <section class="card card-medium" draggable="true" data-card-id="memory-eval-ci">
        <h2>Memory Eval CI</h2>
        {_render_memory_eval_ci(snapshot)}
      </section>

      <section class="card card-medium" draggable="true" data-card-id="operational-projection">
        <h2>Operational Projection</h2>
        <div class="sub">Compiled knowledge projected into recall-only memory.</div>
        {_render_operational_projection(active_intake)}
      </section>

      <section class="card" draggable="true" data-card-id="recall-gaps">
        <h2>Recall Gaps</h2>
        <div class="sub">Low-confidence misses captured for later review.</div>
        <div class="mini-grid">{_render_mini("unknown", recall_gaps.get("unknown_count"))}</div>
        {_render_record(recall_gaps.get("latest"), empty_text="No recall gaps recorded")}
      </section>

      <section class="card" draggable="true" data-card-id="reflections">
        <h2>Reflections</h2>
        <div class="mini-grid">
          {_render_mini("reflections", reflection_stats.get("reflection_count"))}
          {_render_mini("incidents", reflection_stats.get("incident_count"))}
          {_render_mini("unknowns", reflection_stats.get("unknown_count"))}
        </div>
        {_render_kv_table("Tags", reflection_stats.get("tags"))}
      </section>

      <section class="card" draggable="true" data-card-id="source-candidates">
        <h2>Source Candidates</h2>
        <div class="sub">Audit-only intake artifacts; excluded from normal recall.</div>
        <div class="mini-grid">{_render_mini("candidate count", source_candidates.get("count"))}</div>
        {_render_record(source_candidates.get("latest"), empty_text="No source candidates recorded")}
        {_render_record_list(source_candidates.get("list"))}
      </section>

      <section class="card" draggable="true" data-card-id="memory-breakdown">
        <h2>Memory Breakdown</h2>
        {_render_kv_table("By Source", memory_quality.get("by_source"))}
        {_render_kv_table("By Type", memory_quality.get("by_memory_type"))}
      </section>

      <section class="card" draggable="true" data-card-id="backups-health">
        <h2>Backups/Health</h2>
        <div class="mini-grid">
          {_render_mini("backup count", backups.get("count"))}
          {_render_mini("healthy", health.get("ok"))}
        </div>
        {_render_record(backups.get("latest"), empty_text="No backups available")}
      </section>

      <div class="span-3">
        <details>
          <summary>Raw Snapshot JSON</summary>
          <pre>{snapshot_json}</pre>
        </details>
      </div>
    </div>
  </main>
  <script>
    (() => {{
      const storageKey = "eimemory.console.cardOrder.v1";
      const grid = document.querySelector(".grid");
      if (!grid) return;
      const cards = () => Array.from(grid.querySelectorAll("section[data-card-id]"));
      const saved = (() => {{
        try {{ return JSON.parse(localStorage.getItem(storageKey) || "[]"); }}
        catch (_) {{ return []; }}
      }})();
      if (Array.isArray(saved) && saved.length) {{
        const byId = new Map(cards().map((card) => [card.dataset.cardId, card]));
        saved.forEach((id) => {{
          const card = byId.get(id);
          if (card) grid.appendChild(card);
        }});
      }}
      let dragged = null;
      const save = () => localStorage.setItem(storageKey, JSON.stringify(cards().map((card) => card.dataset.cardId)));
      cards().forEach((card) => {{
        card.addEventListener("dragstart", (event) => {{
          dragged = card;
          card.classList.add("dragging");
          event.dataTransfer.effectAllowed = "move";
          event.dataTransfer.setData("text/plain", card.dataset.cardId || "");
        }});
        card.addEventListener("dragend", () => {{
          card.classList.remove("dragging");
          cards().forEach((item) => item.classList.remove("drag-over"));
          dragged = null;
          save();
        }});
        card.addEventListener("dragover", (event) => {{
          if (!dragged || dragged === card) return;
          event.preventDefault();
          card.classList.add("drag-over");
          const rect = card.getBoundingClientRect();
          const after = event.clientY > rect.top + rect.height / 2;
          grid.insertBefore(dragged, after ? card.nextSibling : card);
        }});
        card.addEventListener("dragleave", () => card.classList.remove("drag-over"));
        card.addEventListener("drop", (event) => {{
          event.preventDefault();
          card.classList.remove("drag-over");
          save();
        }});
      }});
      const reset = document.querySelector("[data-reset-layout]");
      if (reset) {{
        reset.addEventListener("click", () => {{
          localStorage.removeItem(storageKey);
          location.reload();
        }});
      }}
    }})();
  </script>
</body>
</html>
"""


def write_evolution_console(snapshot: dict, path: str | Path) -> dict[str, Any]:
    output_path = Path(path)
    html_text = render_evolution_console(snapshot)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(html_text, encoding="utf-8")
    return {
        "ok": True,
        "path": str(output_path),
        "bytes_written": len(html_text.encode("utf-8")),
    }


def _render_kpi(label: str, value: Any) -> str:
    return (
        '<div class="kpi">'
        f'<span class="label">{_escape_text(label)}</span>'
        f'<span class="value">{_escape_text(_display_value(value))}</span>'
        "</div>"
    )


def _render_mini(label: str, value: Any) -> str:
    return (
        '<div class="mini">'
        f'<span class="label">{_escape_text(label)}</span>'
        f'<span class="value">{_escape_text(_display_value(value))}</span>'
        "</div>"
    )


def _render_quality_bars(value: Any) -> str:
    if not isinstance(value, dict) or not value:
        return '<div class="muted">No quality distribution available.</div>'
    total = sum(_safe_int(item) for item in value.values()) or 1
    rows = []
    for key in ("core", "confirmed", "candidate", "rejected"):
        count = _safe_int(value.get(key))
        width = round((count / total) * 100, 2)
        rows.append(
            '<div class="bar-row">'
            f'<span>{_escape_text(key)}</span>'
            f'<div class="bar-track"><div class="bar-fill" style="--width:{width}%"></div></div>'
            f'<strong>{_escape_text(count)}</strong>'
            "</div>"
        )
    return '<div class="bars">' + "".join(rows) + "</div>"


def _render_record(record: Any, *, empty_text: str) -> str:
    if not record:
        return f'<div class="muted">{_escape_text(empty_text)}</div>'
    if not isinstance(record, dict):
        return f'<pre>{_escape_text(record)}</pre>'

    title = _escape_text(record.get("title") or record.get("path") or record.get("record_id") or "record")
    summary = _escape_text(record.get("summary") or _record_fallback_summary(record))
    meta = _escape_text(json.dumps(record.get("meta", {}), ensure_ascii=False, sort_keys=True, indent=2))
    return (
        '<div class="record">'
        f'<div class="record-title">{title}</div>'
        f'<div class="record-summary">{summary}</div>'
        f'<div class="meta">{meta}</div>'
        "</div>"
    )


def _render_record_list(records: Any) -> str:
    if not records:
        return ""
    items = []
    for record in list(records)[:6]:
        items.append(_render_record(record, empty_text=""))
    return '<div class="records">' + "".join(items) + "</div>"


def _render_external_intake(active_intake: dict[str, Any]) -> str:
    report = _normalize_mapping(_nested(active_intake, "external_collection", "latest_report"))
    return (
        '<div class="mini-grid">'
        f'{_render_mini("candidates", active_intake.get("candidate_count"))}'
        f'{_render_mini("open", active_intake.get("open_candidate_count"))}'
        f'{_render_mini("written", report.get("written_count"))}'
        f'{_render_mini("errors", report.get("error_count"))}'
        "</div>"
        f'{_render_report_kv(report, empty_text="No external collection report available.")}'
    )


def _render_paper_promotion(active_intake: dict[str, Any]) -> str:
    report = _normalize_mapping(_nested(active_intake, "paper_promotion", "latest_report"))
    return (
        '<div class="mini-grid">'
        f'{_render_mini("promoted", active_intake.get("promoted_candidate_count"))}'
        f'{_render_mini("papers", active_intake.get("paper_source_count"))}'
        f'{_render_mini("run promoted", report.get("promoted_count"))}'
        f'{_render_mini("skipped", report.get("skipped_count"))}'
        "</div>"
        f'{_render_report_kv(report, empty_text="No paper promotion report available.")}'
    )


def _render_operational_projection(active_intake: dict[str, Any]) -> str:
    projection = _normalize_mapping(active_intake.get("operational_projection"))
    report = _normalize_mapping(projection.get("latest_report"))
    records = projection.get("recent_projected_memories")
    return (
        '<div class="mini-grid">'
        f'{_render_mini("projected", projection.get("projected_memory_count"))}'
        f'{_render_mini("pages", active_intake.get("knowledge_page_count"))}'
        f'{_render_mini("run projected", report.get("projected_count"))}'
        f'{_render_mini("skipped", report.get("skipped_count"))}'
        "</div>"
        f'{_render_report_kv(report, empty_text="No operational projection report available.")}'
        f'{_render_record_list(records)}'
    )


def _render_memory_eval_ci(snapshot: dict[str, Any]) -> str:
    section = _normalize_mapping(snapshot.get("memory_eval_ci"))
    latest = _normalize_mapping(section.get("latest"))
    if not latest:
        return '<div class="muted">No memory eval CI report available.</div>'
    return (
        '<div class="mini-grid">'
        f'{_render_mini("pass rate", latest.get("pass_rate"))}'
        f'{_render_mini("failed", latest.get("fail_count"))}'
        f'{_render_mini("incidents", latest.get("incident_count"))}'
        "</div>"
    )


def _render_recent_papers_candidates(active_intake: dict[str, Any]) -> str:
    candidates = active_intake.get("recent_candidates")
    paper_sources = active_intake.get("recent_paper_sources")
    knowledge_pages = active_intake.get("recent_knowledge_pages")
    if not candidates and not paper_sources and not knowledge_pages:
        return '<div class="muted">No recent paper or candidate records.</div>'
    return (
        f'{_render_labeled_records("Candidates", candidates)}'
        f'{_render_labeled_records("Papers", paper_sources)}'
        f'{_render_labeled_records("Knowledge Pages", knowledge_pages)}'
    )


def _render_labeled_records(title: str, records: Any) -> str:
    if not records:
        return ""
    return f'<div class="kv-title">{_escape_text(title)}</div>{_render_record_list(records)}'


def _render_report_kv(report: dict[str, Any], *, empty_text: str) -> str:
    if not report:
        return f'<div class="muted">{_escape_text(empty_text)}</div>'
    compact = {
        key: value
        for key, value in report.items()
        if key.endswith("_count") or key in {"ok", "promoted_count", "projected_count", "written_count", "skipped_count"}
    }
    return _render_kv_table("Latest Report", compact)


def _render_kv_table(title: str, value: Any) -> str:
    if not value:
        return ""
    rows = []
    if isinstance(value, dict):
        iterable = value.items()
    elif isinstance(value, list):
        iterable = enumerate(value)
    else:
        iterable = [("value", value)]
    for key, item in iterable:
        label = str(key).replace("_", " ").title()
        rows.append(f'<div class="kv-row"><strong>{_escape_text(label)}</strong><span>{_escape_text(item)}</span></div>')
    return f'<div class="kv"><div class="kv-title">{_escape_text(title)}</div>' + "".join(rows) + "</div>"


def _render_warnings(warnings: Any) -> str:
    if not warnings:
        return "All checks green"
    if isinstance(warnings, list):
        return " / ".join(_escape_text(item) for item in warnings[:4])
    return _escape_text(warnings)


def _record_fallback_summary(record: dict[str, Any]) -> str:
    interesting = {
        key: value
        for key, value in record.items()
        if key in {"ok", "verified", "record_count", "sha256", "format_version", "updated_at"}
    }
    if interesting:
        return json.dumps(interesting, ensure_ascii=False, sort_keys=True)
    return ""


def _display_value(value: Any) -> str:
    if value is None:
        return "n/a"
    if isinstance(value, bool):
        return "yes" if value else "no"
    return str(value)


def _nested(payload: dict[str, Any], *keys: str) -> Any:
    current: Any = payload
    for key in keys:
        if not isinstance(current, dict):
            return None
        current = current.get(key)
    return current


def _safe_int(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _normalize_mapping(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _escape_text(value: Any) -> str:
    if value is None:
        return "n/a"
    return html.escape(str(value), quote=True)
