from __future__ import annotations

import asyncio
import json
import logging
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlparse

log = logging.getLogger("lidarr_slsk.ui")

# The page used to show every recent row the status payload included, with no
# size control and no hardcoded count. 10 is the default selection.
RECENT_LIMITS = (10, 25, 50, 100, 250)
DEFAULT_RECENT_LIMIT = 10


def normalize_recent_limit(value: Any) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return DEFAULT_RECENT_LIMIT
    if number in RECENT_LIMITS:
        return number
    return DEFAULT_RECENT_LIMIT


def _recent_options() -> str:
    options = []
    for number in RECENT_LIMITS:
        selected = " selected" if number == DEFAULT_RECENT_LIMIT else ""
        options.append(f'<option value="{number}"{selected}>{number}</option>')
    return "".join(options)


_PAGE_TEMPLATE = """<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8"><title>Lidarr Soulseek</title>
<style>
:root{color-scheme:dark}body{font-family:Segoe UI,system-ui,sans-serif;margin:0;background:#111;color:#eee}
header{padding:16px 20px;background:#1c1c1c;border-bottom:1px solid #333;display:flex;justify-content:space-between}
h1{font-size:18px;margin:0}main{padding:16px 20px}.meta{color:#aaa;font-size:14px}
table{width:100%;border-collapse:collapse;margin-top:12px}
th,td{text-align:left;padding:8px 6px;border-bottom:1px solid #2a2a2a;font-size:13px}
button{background:#8b1e1e;color:#fff;border:0;padding:6px 10px;border-radius:4px;cursor:pointer}
button.secondary{background:#333}.ok{color:#7dce7d}.bad{color:#e07a7a}
.bar{height:8px;background:#333;border-radius:4px;overflow:hidden;min-width:80px}.bar>i{display:block;height:100%;background:#3d8bfd}
h2{font-size:15px;margin:28px 0 8px}
.recent-head{display:flex;align-items:center;justify-content:space-between;gap:12px;margin-top:28px}
.recent-head h2{margin:0}
label.recent-limit{display:flex;align-items:center;gap:8px}
select{background:#1c1c1c;color:#eee;border:1px solid #444;border-radius:4px;padding:4px 8px;font-size:13px}
</style></head><body>
<header><h1>Lidarr Soulseek</h1><div>
<button class="secondary" onclick="load()">Refresh</button>
<button onclick="cancelAll()">Cancel all</button></div></header>
<main><div id="job" class="meta">Loading…</div>
<div id="detail" class="meta"></div>
<table><thead><tr><th>File</th><th>User</th><th>Status</th><th>Progress</th><th></th></tr></thead><tbody id="rows"></tbody></table>
<div class="recent-head"><h2>Recent completed</h2>
<label class="recent-limit meta" for="recent-limit">Show
<select id="recent-limit" onchange="setRecentLimit(this.value)">%%RECENT_OPTIONS%%</select>
</label></div>
<table><thead><tr><th>When</th><th>Artist / album</th><th>File</th><th>User</th></tr></thead><tbody id="recent"></tbody></table>
</main>
<script>
const RECENT_LIMITS = %%RECENT_LIMITS_JSON%%;
const DEFAULT_RECENT_LIMIT = %%DEFAULT_RECENT_LIMIT%%;
function recentLimit(){
  try {
    const n = parseInt(sessionStorage.getItem('recentFileLimit') || '', 10);
    if (RECENT_LIMITS.includes(n)) return n;
  } catch (e) {}
  return DEFAULT_RECENT_LIMIT;
}
function setRecentLimit(value){
  const n = parseInt(value, 10);
  if (!RECENT_LIMITS.includes(n)) return;
  try { sessionStorage.setItem('recentFileLimit', String(n)); } catch (e) {}
  load();
}
async function load(){
  const limit = recentLimit();
  const sel = document.getElementById('recent-limit');
  if (sel && document.activeElement !== sel) sel.value = String(limit);
  const r = await fetch('/api/status?limit=' + encodeURIComponent(limit)); const d = await r.json(); const job = d.job||{};
  const activity = d.activity || [job.phase||'idle',[job.artist,job.title].filter(Boolean).join(' \\u2014 ')].filter(Boolean).join(' \\u00b7 ');
  const elapsed = d.elapsed_text ? (' \\u00b7 ' + d.elapsed_text) : '';
  const mark = d.busy ? '\\u25cf ' : '\\u25cb ';
  const link = d.connected_label || (d.logged_in ? 'Soulseek connected' : 'Soulseek offline');
  document.getElementById('job').textContent = mark + [link, activity].filter(Boolean).join(' \\u00b7 ') + elapsed;
  const detail = document.getElementById('detail');
  if (detail) detail.textContent = d.soulseek_detail || '';
  const body=document.getElementById('rows'); body.innerHTML='';
  (d.transfers||[]).forEach((t,i)=>{
    const tr=document.createElement('tr');
    const width=t.percent==null?0:Math.max(0,Math.min(100,t.percent));
    const pct=t.percent==null?'':Math.round(t.percent)+'%';
    const done=['COMPLETE','FAILED','ABORTED'].includes(t.state);
    tr.innerHTML=`<td>${esc(t.filename||'file')}</td><td>${esc(t.username||'')}</td><td class="${t.state==='COMPLETE'?'ok':(t.state==='FAILED'||t.state==='ABORTED'?'bad':'')}">${esc(t.state||'')}</td><td><div class="bar"><i style="width:${width}%"></i></div>${pct}</td><td>${done?'':`<button onclick="cancelOne(${i})">Cancel</button>`}</td>`;
    body.appendChild(tr);
  });
  if(!(d.transfers||[]).length){const tr=document.createElement('tr'); tr.innerHTML='<td colspan="5" class="meta">No active downloads</td>'; body.appendChild(tr);}
  const items=(d.recent||[]).slice(0, limit);
  const recent=document.getElementById('recent'); recent.innerHTML='';
  items.forEach(item=>{
    const tr=document.createElement('tr');
    tr.innerHTML=`<td class="meta">${esc(fmtTime(item.finished_at))}</td><td>${esc([item.artist,item.title].filter(Boolean).join(' \\u2014 '))}</td><td>${esc(item.filename||'')}</td><td>${esc(item.username||'')}</td>`;
    recent.appendChild(tr);
  });
  if(!items.length){const tr=document.createElement('tr'); tr.innerHTML='<td colspan="4" class="meta">Nothing finished yet this install</td>'; recent.appendChild(tr);}
}
function fmtTime(iso){if(!iso)return ''; const d=new Date(iso); return Number.isNaN(d.getTime())?iso:d.toLocaleString();}
function esc(s){return String(s).replace(/[&<>"'`]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;','`':'&#96;'}[c]));}
async function cancelOne(i){await fetch('/api/cancel',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({index:i})}); load();}
async function cancelAll(){await fetch('/api/cancel',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({all:true})}); load();}
load(); setInterval(load,2000);
</script></body></html>
"""

PAGE = (
    _PAGE_TEMPLATE.replace("%%RECENT_OPTIONS%%", _recent_options())
    .replace("%%RECENT_LIMITS_JSON%%", json.dumps(list(RECENT_LIMITS)))
    .replace("%%DEFAULT_RECENT_LIMIT%%", str(DEFAULT_RECENT_LIMIT))
)


class StatusHandler(BaseHTTPRequestHandler):
    worker: Any = None
    loop: asyncio.AbstractEventLoop | None = None

    def log_message(self, fmt: str, *args: Any) -> None:
        log.debug("%s - " + fmt, self.address_string(), *args)

    def _send(self, status: int, body: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _status_payload(self, limit: int) -> dict:
        snapshot = getattr(self.worker, "ui_snapshot", None)
        payload: Any = None
        if callable(snapshot):
            try:
                payload = snapshot(recent_limit=limit)
            except TypeError:
                payload = snapshot()
        if not isinstance(payload, dict):
            payload = {}
        else:
            payload = dict(payload)
        payload["recent"] = list(payload.get("recent") or [])[:limit]
        return payload

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        path = parsed.path
        if path in {"/", "/index.html"}:
            self._send(200, PAGE.encode("utf-8"), "text/html; charset=utf-8")
            return
        if path == "/api/status":
            raw_limit = (parse_qs(parsed.query).get("limit") or [None])[0]
            limit = normalize_recent_limit(raw_limit)
            self._send(200, json.dumps(self._status_payload(limit)).encode("utf-8"), "application/json")
            return
        self._send(404, b"not found", "text/plain")

    def do_POST(self) -> None:
        if urlparse(self.path).path != "/api/cancel":
            self._send(404, b"not found", "text/plain")
            return
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b"{}"
        try:
            body = json.loads(raw.decode("utf-8") or "{}")
        except json.JSONDecodeError:
            body = {}
        loop = getattr(self.worker, "_loop", None) or self.loop
        if loop is None:
            self._send(503, b'{"ok":false,"error":"worker is not running"}', "application/json")
            return
        fut = asyncio.run_coroutine_threadsafe(
            self.worker.cancel_downloads(all_items=bool(body.get("all")), index=body.get("index")),
            loop,
        )
        try:
            fut.result(timeout=15)
        except Exception as exc:
            log.exception("Cancel failed: %s", exc)
            self._send(500, json.dumps({"ok": False, "error": str(exc)}).encode(), "application/json")
            return
        self._send(200, b'{"ok":true}', "application/json")


def start_status_ui(worker: Any, host: str, port: int) -> ThreadingHTTPServer:
    handler = type("BoundStatusHandler", (StatusHandler,), {})
    handler.worker = worker
    handler.loop = asyncio.get_event_loop()
    server = ThreadingHTTPServer((host, port), handler)
    threading.Thread(target=server.serve_forever, name="status-ui", daemon=True).start()
    log.info("Status page http://%s:%s", host, port)
    return server
