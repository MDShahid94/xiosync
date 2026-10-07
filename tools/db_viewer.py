#!/usr/bin/env python3
"""
XIOSYNC Database Viewer
=======================
Dumps all public tables from the XIOSYNC PostgreSQL database into a
beautiful, interactive HTML page and opens it in your default browser.

Usage:
    python3 tools/db_viewer.py
    python3 tools/db_viewer.py --table identities credentials
    python3 tools/db_viewer.py --db postgresql://karmareturns@localhost:5432/xiosync
    python3 tools/db_viewer.py --no-open        # write HTML without opening browser
    python3 tools/db_viewer.py --out /tmp/db.html

Requires: psycopg2 or psycopg (whichever is installed)
"""

from __future__ import annotations

import argparse
import html
import json
import os
import re
import tempfile
import webbrowser
from datetime import datetime
from pathlib import Path

DB_URL = os.environ.get(
    "DATABASE_URL",
    "postgresql://karmareturns@localhost:5432/xiosync",
)

EXCLUDE_TABLES = {"alembic_version"}
MAX_ROWS = 500

PALETTE = {
    "identit": "#6366f1",
    "credential": "#8b5cf6",
    "vault": "#a855f7",
    "mesh": "#0ea5e9",
    "xioflow": "#f59e0b",
    "xiogrid": "#ef4444",
    "storage": "#10b981",
    "session": "#14b8a6",
    "actor": "#f97316",
    "org": "#ec4899",
    "member": "#ec4899",
    "plugin": "#84cc16",
    "domain": "#06b6d4",
    "workflow": "#f59e0b",
    "worker": "#6b7280",
    "event": "#d946ef",
    "profile": "#6366f1",
    "browser": "#3b82f6",
    "project": "#22c55e",
    "runtime": "#64748b",
    "compute": "#64748b",
    "type": "#a16207",
    "registry": "#a16207",
    "memory": "#f43f5e",
    "document": "#0284c7",
    "artifact": "#7c3aed",
    "grant": "#15803d",
    "edge": "#6d28d9",
    "operation": "#b45309",
    "usage": "#0891b2",
    "webhook": "#be185d",
    "secret": "#9f1239",
    "integration": "#0d9488",
    "capability": "#1d4ed8",
    "resource": "#065f46",
    "default": "#64748b",
}


def badge_colour(name: str) -> str:
    for prefix, colour in PALETTE.items():
        if name.startswith(prefix):
            return colour
    return PALETTE["default"]


# ── DB helpers ────────────────────────────────────────────────────────────────


def connect(url: str):
    try:
        import psycopg

        return psycopg.connect(url.replace("postgresql+psycopg://", "postgresql://"))
    except ImportError:
        pass
    try:
        import psycopg2

        return psycopg2.connect(url.replace("postgresql+psycopg://", "postgresql://"))
    except ImportError:
        pass
    raise SystemExit("❌ No psycopg/psycopg2 found. Install with: pip install psycopg2-binary")


def get_tables(cur, only: list[str] | None) -> list[str]:
    cur.execute("""
        SELECT table_name FROM information_schema.tables
        WHERE table_schema='public' AND table_type='BASE TABLE'
        ORDER BY table_name
    """)
    tables = [r[0] for r in cur.fetchall() if r[0] not in EXCLUDE_TABLES]
    if only:
        tables = [t for t in tables if t in only]
    return tables


def get_columns(cur, table: str) -> list[tuple[str, str]]:
    cur.execute(
        """
        SELECT column_name, udt_name FROM information_schema.columns
        WHERE table_schema='public' AND table_name=%s
        ORDER BY ordinal_position
    """,
        [table],
    )
    return [(r[0], r[1]) for r in cur.fetchall()]


def get_rows(cur, table: str, limit: int) -> tuple[int, list]:
    try:
        cur.execute(f'SELECT COUNT(*) FROM "{table}"')
        total = cur.fetchone()[0]
        cur.execute(f'SELECT * FROM "{table}" LIMIT %s', [limit])
        return total, cur.fetchall()
    except Exception:
        return 0, []


# ── HTML rendering ─────────────────────────────────────────────────────────────


def cell_html(val) -> str:
    if val is None:
        return '<span class="null">NULL</span>'
    s = str(val)
    if s.startswith(("{", "[")):
        try:
            pretty = json.dumps(json.loads(s), indent=2, ensure_ascii=False)
            return (
                f'<span class="json" title="{html.escape(pretty)}">'
                f"{html.escape(s[:100])}{'…' if len(s) > 100 else ''}</span>"
            )
        except Exception:
            pass
    if re.match(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", s, re.I):
        return f'<span class="uuid" title="{html.escape(s)}">{s[:8]}…</span>'
    if re.match(r"^\d{4}-\d{2}-\d{2}", s):
        return f'<span class="ts">{html.escape(s[:19])}</span>'
    if len(s) > 140:
        return f'<span title="{html.escape(s)}">{html.escape(s[:120])}…</span>'
    return html.escape(s)


TYPE_CLS = {
    "uuid": "b-uuid",
    "text": "b-text",
    "varchar": "b-text",
    "bpchar": "b-text",
    "int4": "b-int",
    "int8": "b-int",
    "bool": "b-bool",
    "jsonb": "b-json",
    "json": "b-json",
    "timestamptz": "b-ts",
    "timestamp": "b-ts",
    "float8": "b-float",
    "numeric": "b-float",
    "bytea": "b-bytes",
}


def type_badge(udt: str) -> str:
    cls = TYPE_CLS.get(udt, "b-def")
    return f'<span class="tb {cls}">{html.escape(udt)}</span>'


def render_table_section(table: str, columns: list, total: int, rows: list) -> str:
    colour = badge_colour(table)
    headers = "".join(f"<th>{html.escape(c)}{type_badge(t)}</th>" for c, t in columns)
    body = "".join(
        "<tr>" + "".join(f"<td>{cell_html(v)}</td>" for v in row) + "</tr>\n" for row in rows
    )
    trunc = (
        f'<div class="trunc">Showing first {MAX_ROWS:,} of {total:,} rows</div>'
        if total > MAX_ROWS
        else ""
    )
    return f"""
<section class="ts" id="tbl-{table}">
  <div class="th" style="border-left:4px solid {colour}">
    <div class="tt">
      <span class="tn">{html.escape(table)}</span>
      <span class="rb" style="background:{colour}">{total:,}</span>
      <span class="cc">{len(columns)} cols</span>
    </div>
    <div class="ta">
      <input class="sb" type="text" placeholder="🔍 filter…" oninput="ft(this)">
      <button class="bc" onclick="csv('{table}')">⬇ CSV</button>
      <button class="bx" onclick="tog(this)">▲</button>
    </div>
  </div>
  <div class="tb2">{trunc}<div class="sc">
    <table class="dt" id="dt-{table}">
      <thead><tr>{headers}</tr></thead>
      <tbody>{body}</tbody>
    </table>
  </div></div>
</section>"""


def render_nav(table: str, total: int) -> str:
    c = badge_colour(table)
    return (
        f'<li class="ni" onclick="go(\'{table}\')">'
        f'<span class="nd" style="background:{c}"></span>'
        f'<span class="nn">{html.escape(table)}</span>'
        f'<span class="nc">{total:,}</span></li>'
    )


# ── CSS + JS (minified inline) ─────────────────────────────────────────────────

CSS = """
:root{--bg:#0f172a;--s:#1e293b;--s2:#334155;--br:#475569;--t:#f1f5f9;--m:#94a3b8;--a:#6366f1}
[data-theme=light]{--bg:#f8fafc;--s:#fff;--s2:#f1f5f9;--br:#e2e8f0;--t:#0f172a;--m:#64748b}
*{box-sizing:border-box;margin:0;padding:0}
body{background:var(--bg);color:var(--t);font-family:'Inter',system-ui,sans-serif;font-size:13px;display:flex;height:100vh;overflow:hidden}
#sb{width:255px;min-width:255px;background:var(--s);border-right:1px solid var(--br);display:flex;flex-direction:column;overflow:hidden}
#sbh{padding:14px;border-bottom:1px solid var(--br)}
#sbh h1{font-size:15px;font-weight:700;color:var(--a)}
#sbh p{font-size:11px;color:var(--m);margin-top:3px}
#sbs{padding:8px 10px;border-bottom:1px solid var(--br)}
#sbs input{width:100%;background:var(--s2);border:1px solid var(--br);border-radius:6px;padding:5px 9px;color:var(--t);font-size:12px;outline:none}
#nl{overflow-y:auto;flex:1;padding:6px 0;list-style:none}
.ni{display:flex;align-items:center;gap:7px;padding:6px 12px;cursor:pointer;border-radius:6px;margin:1px 5px;transition:background .1s}
.ni:hover,.ni.active{background:var(--s2)}
.nd{width:7px;height:7px;border-radius:50%;flex-shrink:0}
.nn{flex:1;font-size:12px;font-weight:500;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.nc{font-size:11px;color:var(--m);font-variant-numeric:tabular-nums}
#main{flex:1;overflow-y:auto;padding:14px 18px}
#top{display:flex;align-items:center;justify-content:space-between;margin-bottom:14px}
#top h2{font-size:17px;font-weight:700}
#top p{font-size:11px;color:var(--m)}
#thm{background:var(--s);border:1px solid var(--br);border-radius:6px;padding:5px 11px;color:var(--t);cursor:pointer;font-size:12px}
.ts{background:var(--s);border:1px solid var(--br);border-radius:10px;margin-bottom:14px;overflow:hidden}
.th{display:flex;align-items:center;justify-content:space-between;padding:10px 14px;background:var(--s2);flex-wrap:wrap;gap:8px}
.tt{display:flex;align-items:center;gap:8px}
.tn{font-size:14px;font-weight:700;font-family:'JetBrains Mono',monospace}
.rb{font-size:11px;font-weight:600;color:#fff;padding:2px 8px;border-radius:20px}
.cc{font-size:11px;color:var(--m)}
.ta{display:flex;align-items:center;gap:7px}
.sb{background:var(--s);border:1px solid var(--br);border-radius:6px;padding:4px 9px;color:var(--t);font-size:12px;width:190px;outline:none}
.sb:focus{border-color:var(--a)}
.bc,.bx{background:transparent;border:1px solid var(--br);color:var(--m);border-radius:6px;padding:4px 9px;cursor:pointer;font-size:11px}
.bc:hover,.bx:hover{color:var(--t);border-color:var(--t)}
.trunc{font-size:11px;color:var(--m);padding:5px 14px;background:var(--s2);border-bottom:1px solid var(--br)}
.sc{overflow-x:auto}
.dt{width:100%;border-collapse:collapse;font-size:12px}
.dt thead th{position:sticky;top:0;background:var(--s2);padding:7px 11px;text-align:left;font-weight:600;white-space:nowrap;border-bottom:2px solid var(--br);color:var(--m);z-index:1}
.dt tbody tr{border-bottom:1px solid var(--br);transition:background .1s}
.dt tbody tr:hover{background:var(--s2)}
.dt td{padding:6px 11px;vertical-align:top;max-width:300px;word-break:break-word}
.dt tbody tr:last-child{border-bottom:none}
.hr{display:none}
.tb{font-size:9px;font-weight:600;border-radius:3px;padding:1px 4px;margin-left:4px;opacity:.75;vertical-align:middle}
.b-uuid{background:#4c1d95;color:#c4b5fd}.b-text{background:#1e3a5f;color:#93c5fd}
.b-int{background:#422006;color:#fdba74}.b-bool{background:#052e16;color:#86efac}
.b-json{background:#064e3b;color:#6ee7b7}.b-ts{background:#0c4a6e;color:#7dd3fc}
.b-float{background:#3b0764;color:#d8b4fe}.b-bytes{background:#450a0a;color:#fca5a5}
.b-def{background:var(--s2);color:var(--m)}
.null{color:#ef4444;font-style:italic;font-size:11px}
.uuid{color:#a78bfa;font-family:'JetBrains Mono',monospace;font-size:11px;cursor:help}
.ts2{color:#22d3ee;font-family:'JetBrains Mono',monospace;font-size:11px}
.json{color:#34d399;font-family:'JetBrains Mono',monospace;font-size:11px;cursor:help}
#sbf{padding:8px 12px;border-top:1px solid var(--br);font-size:11px;color:var(--m)}
::-webkit-scrollbar{width:5px;height:5px}
::-webkit-scrollbar-track{background:var(--s)}
::-webkit-scrollbar-thumb{background:var(--br);border-radius:3px}
"""

JS = r"""
function go(t){
  document.getElementById('tbl-'+t)?.scrollIntoView({behavior:'smooth',block:'start'});
  document.querySelectorAll('.ni').forEach(e=>e.classList.toggle('active',e.getAttribute('onclick').includes("'"+t+"'")));
}
function ft(inp){
  const rows=inp.closest('.ts').querySelectorAll('tbody tr');
  const q=inp.value.toLowerCase();
  rows.forEach(r=>r.classList.toggle('hr',!!q&&!r.textContent.toLowerCase().includes(q)));
}
function tog(btn){
  const b=btn.closest('.ts').querySelector('.tb2');
  const c=b.style.display==='none';
  b.style.display=c?'':'none';btn.textContent=c?'▲':'▼';
}
function csv(t){
  const tbl=document.getElementById('dt-'+t);
  if(!tbl)return;
  const hdr=[...tbl.querySelectorAll('thead th')].map(th=>'"'+th.textContent.replace(/[\u0000-\u001f]/g,'').replace(/"/g,'""')+'"');
  const rows=[hdr.join(',')];
  tbl.querySelectorAll('tbody tr:not(.hr)').forEach(tr=>{
    rows.push([...tr.querySelectorAll('td')].map(td=>'"'+td.textContent.replace(/"/g,'""').trim()+'"').join(','));
  });
  const a=document.createElement('a');
  a.href=URL.createObjectURL(new Blob([rows.join('\n')],{type:'text/csv'}));
  a.download=t+'.csv';a.click();
}
function navFilter(inp){
  const q=inp.value.toLowerCase();
  document.querySelectorAll('.ni').forEach(e=>{
    e.style.display=!q||e.querySelector('.nn').textContent.toLowerCase().includes(q)?'':'none';
  });
}
function toggleTheme(){
  const d=document.documentElement,c=d.getAttribute('data-theme');
  d.setAttribute('data-theme',c==='light'?'dark':'light');
  document.getElementById('thm').textContent=c==='light'?'☀️ Light':'🌙 Dark';
}
document.addEventListener('DOMContentLoaded',()=>{
  const obs=new IntersectionObserver(es=>{
    es.forEach(e=>{if(e.isIntersecting){const id=e.target.id.replace('tbl-','');
      document.querySelectorAll('.ni').forEach(el=>el.classList.toggle('active',!!el.getAttribute('onclick')?.includes("'"+id+"'")));
    }});
  },{threshold:.1,rootMargin:'-40px 0px -80% 0px'});
  document.querySelectorAll('.ts').forEach(s=>obs.observe(s));
});
"""


def build_html(sections, nav_items, n_tables, total_rows, ts) -> str:
    return f"""<!DOCTYPE html>
<html lang="en" data-theme="dark">
<head>
<meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>XIOSYNC · DB Viewer</title>
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&family=JetBrains+Mono:wght@400;600&display=swap" rel="stylesheet">
<style>{CSS}</style>
</head>
<body>
<aside id="sb">
  <div id="sbh">
    <h1>⚡ XIOSYNC</h1>
    <p>{n_tables} tables · {total_rows:,} total rows</p>
    <p>{ts}</p>
  </div>
  <div id="sbs"><input type="text" placeholder="🔍 search tables…" oninput="navFilter(this)"></div>
  <ul id="nl">{"".join(nav_items)}</ul>
  <div id="sbf">postgresql · xiosync · localhost</div>
</aside>
<div id="main">
  <div id="top">
    <div><h2>Database Explorer</h2><p>{n_tables} tables · {total_rows:,} rows · {ts}</p></div>
    <button id="thm" onclick="toggleTheme()">☀️ Light</button>
  </div>
  {"".join(sections)}
</div>
<script>{JS}</script>
</body></html>"""


# ── Entry point ───────────────────────────────────────────────────────────────


def main() -> None:
    p = argparse.ArgumentParser(description="XIOSYNC DB Viewer")
    p.add_argument("--db", default=DB_URL)
    p.add_argument("--table", nargs="*", help="Specific tables only")
    p.add_argument("--no-open", action="store_true")
    p.add_argument("--out", help="Output HTML path")
    p.add_argument("--limit", type=int, default=MAX_ROWS)
    args = p.parse_args()

    print(f"🔌 Connecting to {args.db.split('@')[-1]} …")
    conn = connect(args.db)
    cur = conn.cursor()

    try:
        cur.execute(
            "SELECT set_config('app.current_org_id','00000000-0000-7000-8000-000000000000',true)"
        )
        cur.execute(
            "SELECT set_config('app.current_org',   '00000000-0000-7000-8000-000000000000',true)"
        )
    except Exception:
        pass

    tables = get_tables(cur, args.table)
    print(f"📋 Found {len(tables)} tables. Fetching …\n")

    sections, nav_items, total_rows = [], [], 0

    for i, tbl in enumerate(tables):
        print(f"  {i + 1:02d}/{len(tables)}  {tbl:<45}", end="", flush=True)
        cols = get_columns(cur, tbl)
        total, rows = get_rows(cur, tbl, args.limit)
        total_rows += total
        print(f"{total:>8,} rows")
        sections.append(render_table_section(tbl, cols, total, rows))
        nav_items.append(render_nav(tbl, total))

    cur.close()
    conn.close()

    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    content = build_html(sections, nav_items, len(tables), total_rows, ts)

    if args.out:
        out = Path(args.out)
    else:
        fd, tmp = tempfile.mkstemp(suffix=".html", prefix="xiosync_db_")
        os.close(fd)
        out = Path(tmp)

    out.write_text(content, encoding="utf-8")
    size_kb = out.stat().st_size // 1024
    print(f"\n✅  {out}  ({size_kb:,} KB)")

    if not args.no_open:
        webbrowser.open(out.as_uri())
        print("🌐  Opened in browser.")


if __name__ == "__main__":
    main()
