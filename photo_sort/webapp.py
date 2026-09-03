"""One local web page that drives the whole thing: Scan, Review, Apply -- with
live progress. No terminal needed after ``photo-sort ui``.

Long jobs (scan, apply) run in a background thread inside this process; the page
polls ``/api/progress`` once a second.
"""

from __future__ import annotations

import threading
import time
import traceback
import webbrowser
from pathlib import Path

import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from rich.console import Console

# ---- background job state -------------------------------------------------
_LOCK = threading.Lock()
_JOB: dict = {
    "kind": None,        # "scan" | "apply" | None
    "state": "idle",     # "idle" | "running" | "done" | "error"
    "total": 0,
    "done": 0,
    "message": "",
    "started_at": 0.0,
    "finished_at": 0.0,
    "summary": None,      # dict on success
    "error": None,
}


def _reset(kind: str) -> None:
    with _LOCK:
        _JOB.update(
            kind=kind, state="running", total=0, done=0, message="starting…",
            started_at=time.time(), finished_at=0.0, summary=None, error=None,
        )


def _progress(done: int, total: int, message: str) -> None:
    with _LOCK:
        _JOB.update(done=done, total=total, message=message)


def _finish(summary: dict | None = None, error: str | None = None) -> None:
    with _LOCK:
        _JOB.update(
            state="error" if error else "done",
            finished_at=time.time(), summary=summary, error=error,
        )


def _snapshot() -> dict:
    with _LOCK:
        j = dict(_JOB)
    elapsed = (j["finished_at"] or time.time()) - j["started_at"] if j["started_at"] else 0.0
    pct = (j["done"] / j["total"] * 100.0) if j["total"] else 0.0
    eta = (elapsed / j["done"] * (j["total"] - j["done"])) if j["done"] else 0.0
    j["elapsed_s"] = round(elapsed)
    j["eta_s"] = round(eta)
    j["percent"] = round(pct, 1)
    return j


def _run_scan_job(config_path: str, state_dir: Path, similarity: int, refresh: bool) -> None:
    from photo_sort.pipeline import run_scan

    _reset("scan")
    try:
        summary = run_scan(
            config_path=config_path, thumb_px=256, refresh=refresh,
            state_dir=state_dir, console=None, similarity=similarity,
            progress_cb=_progress,
        )
        _finish(summary=summary)
    except Exception:  # noqa: BLE001 - surface it to the page
        _finish(error=traceback.format_exc())


def _run_apply_job(config_path: str, state_dir: Path) -> None:
    from photo_sort.pipeline import run_apply

    _reset("apply")
    try:
        result = run_apply(
            state_dir=state_dir, assume_yes=True, console=None,
            config_path=config_path, progress_cb=_progress,
        )
        _finish(summary=result)
    except Exception:  # noqa: BLE001
        _finish(error=traceback.format_exc())


# ---- server -------------------------------------------------
def serve(
    state_dir: Path, host: str = "127.0.0.1", port: int = 8000,
    config_path: str = "photo-sort.toml", console: Console | None = None,
    open_browser: bool = True,
) -> None:
    from photo_sort.config import Config
    from photo_sort.store import Store, photo_key

    console = console or Console()
    store = Store(state_dir)
    app = FastAPI()

    @app.get("/", response_class=HTMLResponse)
    def index() -> str:
        return PAGE

    @app.get("/api/config")
    def api_config() -> JSONResponse:
        try:
            cfg = Config.load(config_path)
            return JSONResponse({"ok": True, "sources": [
                {"type": s.type, "roots": s.roots} for s in cfg.sources
            ]})
        except Exception as e:  # noqa: BLE001
            return JSONResponse({"ok": False, "error": str(e)})

    @app.get("/api/progress")
    def api_progress() -> JSONResponse:
        snap = _snapshot()
        try:
            store.load_scan()
            snap["has_scan"] = True
        except FileNotFoundError:
            snap["has_scan"] = False
        return JSONResponse(snap)

    @app.post("/api/scan")
    async def api_scan(request: Request) -> JSONResponse:
        body = {}
        try:
            body = await request.json()
        except Exception:  # noqa: BLE001 - empty body is fine
            pass
        with _LOCK:
            if _JOB["state"] == "running":
                return JSONResponse({"ok": False, "error": "a job is already running"}, status_code=409)
        similarity = int(body.get("similarity", 12))
        refresh = bool(body.get("refresh", False))
        threading.Thread(
            target=_run_scan_job, args=(config_path, state_dir, similarity, refresh), daemon=True,
        ).start()
        return JSONResponse({"ok": True})

    @app.post("/api/apply")
    def api_apply() -> JSONResponse:
        with _LOCK:
            if _JOB["state"] == "running":
                return JSONResponse({"ok": False, "error": "a job is already running"}, status_code=409)
        threading.Thread(
            target=_run_apply_job, args=(config_path, state_dir), daemon=True,
        ).start()
        return JSONResponse({"ok": True})

    @app.get("/api/result")
    def api_result() -> JSONResponse:
        try:
            return JSONResponse({"ok": True, **store.load_scan()})
        except FileNotFoundError:
            return JSONResponse({"ok": False, "groups": [], "photos": []})

    @app.post("/api/regroup")
    async def api_regroup(request: Request) -> JSONResponse:
        """Re-group the existing scan at a new strictness. No network; instant."""
        from photo_sort.pipeline import regroup

        body = {}
        try:
            body = await request.json()
        except Exception:  # noqa: BLE001
            pass
        try:
            summary = regroup(state_dir, int(body.get("similarity", 12)))
            return JSONResponse({"ok": True, "summary": summary})
        except FileNotFoundError as e:
            return JSONResponse({"ok": False, "error": str(e)}, status_code=400)

    @app.get("/api/decisions")
    def api_get_decisions() -> JSONResponse:
        return JSONResponse(store.load_decisions())

    @app.post("/api/decisions")
    async def api_set_decisions(request: Request) -> JSONResponse:
        body = await request.json()
        store.save_decisions(body)
        return JSONResponse({"saved": len(body)})

    @app.get("/thumb")
    def thumb(source_id: str, id: str):  # noqa: A002 - matches query param
        p = Path(store.thumb_path_for(photo_key(source_id, id)))
        if not p.exists():
            return JSONResponse({"error": "no thumb"}, status_code=404)
        return FileResponse(p, media_type="image/jpeg")

    url = f"http://{host}:{port}"
    console.print(f"photo-sort UI at [bold]{url}[/]   (Ctrl-C to stop)")
    if open_browser:
        threading.Timer(0.8, lambda: webbrowser.open(url)).start()
    uvicorn.run(app, host=host, port=port, log_level="warning")


PAGE = r"""<!doctype html><html><head><meta charset=utf-8><title>photo-sort</title>
<style>
 :root{color-scheme:light dark}
 body{font:14px/1.5 system-ui,sans-serif;margin:0;background:#f4f4f2;color:#181818}
 @media (prefers-color-scheme:dark){body{background:#161616;color:#e8e8e8}}
 header{padding:14px 20px;font-weight:700;font-size:16px;border-bottom:1px solid #0002}
 .wrap{max-width:1100px;margin:0 auto;padding:16px 20px}
 .card{background:#fff;border:1px solid #0001;border-radius:10px;padding:16px;margin:14px 0}
 @media (prefers-color-scheme:dark){.card{background:#222;border-color:#fff2}}
 h2{margin:0 0 10px;font-size:14px;letter-spacing:.02em;text-transform:uppercase;color:#888}
 button{font:inherit;padding:8px 16px;border:1px solid #888;border-radius:7px;background:#fff;cursor:pointer}
 button.primary{background:#1a7a4c;color:#fff;border-color:#1a7a4c}
 button:disabled{opacity:.4;cursor:not-allowed}
 @media (prefers-color-scheme:dark){button{background:#333;color:#eee}}
 .bar{height:12px;background:#0001;border-radius:6px;overflow:hidden;margin:10px 0}
 .bar>i{display:block;height:100%;background:#1a7a4c;width:0;transition:width .3s}
 .stat{display:flex;gap:22px;flex-wrap:wrap;font-variant-numeric:tabular-nums;color:#555}
 @media (prefers-color-scheme:dark){.stat{color:#aaa}}
 .stat b{color:inherit;font-weight:700}
 .muted{color:#888}
 pre{white-space:pre-wrap;background:#0001;padding:10px;border-radius:6px;font-size:12px;overflow:auto;max-height:220px}
 .group{border:1px solid #0001;border-radius:8px;padding:10px;margin:10px 0}
 .group h3{margin:0 0 8px;font-size:12px;color:#888;font-weight:600}
 .row{display:flex;flex-wrap:wrap;gap:10px}
 .ph{width:190px;border:2px solid transparent;border-radius:6px;padding:6px;background:#0000000a}
 .ph.remove{border-color:#d33;background:#d3330f14}
 .ph.keep{border-color:#1a7a4c}
 .ph img{width:100%;height:140px;object-fit:cover;border-radius:4px;background:#8883}
 .ph .n{font-size:11px;word-break:break-all;margin:4px 0}
 .ph .fl{font-size:11px;color:#b60}
 .ph label{font-size:12px;display:flex;gap:5px;align-items:center}
</style></head><body>
<header>photo-sort</header>
<div class=wrap>

 <div class=card id=cfgcard>
  <h2>Sources</h2>
  <div id=cfg class=muted>loading…</div>
 </div>

 <div class=card>
  <h2>1 &middot; Scan</h2>
  <div class=stat style=margin-bottom:10px>
   <label>match strictness
    <select id=sim>
     <option value=8>strict (copies only)</option>
     <option value=12 selected>normal</option>
     <option value=16>loose</option>
    </select>
   </label>
   <label><input type=checkbox id=refresh> re-scan everything (ignore cache)</label>
  </div>
  <button class=primary id=scanBtn onclick=startScan()>Scan now</button>
  <div id=scanProg hidden>
   <div class=bar><i id=scanFill></i></div>
   <div class=stat>
    <span><b id=pDone>0</b> / <b id=pTotal>0</b> photos</span>
    <span><b id=pPct>0</b>%</span>
    <span>elapsed <b id=pEl>0:00</b></span>
    <span>ETA <b id=pEta>–</b></span>
   </div>
   <div class=muted id=pMsg></div>
  </div>
  <div id=scanDone hidden></div>
  <pre id=scanErr hidden></pre>
 </div>

 <div class=card>
  <h2>2 &middot; Review</h2>
  <div class=stat style=margin-bottom:10px>
   <label>match strictness
    <select id=sim2>
     <option value=8>strict (copies only)</option>
     <option value=12 selected>normal</option>
     <option value=16>loose</option>
    </select>
   </label>
   <button id=regroupBtn disabled onclick=regroup()>Re-group at this strictness</button>
   <span class=muted>instant — no re-scan</span>
  </div>
  <button id=revBtn disabled onclick=loadReview()>Open review</button>
  <button id=saveBtn hidden onclick=saveDecisions()>Save decisions</button>
  <span id=revSummary class=muted></span>
  <div id=review></div>
 </div>

 <div class=card>
  <h2>3 &middot; Apply</h2>
  <p class=muted>Moves every photo you ticked into a <b>photo-sort review</b> folder in the source. Never deletes.</p>
  <button id=applyBtn disabled onclick=startApply()>Move ticked photos</button>
  <div id=applyProg hidden>
   <div class=bar><i id=applyFill></i></div>
   <div class=stat><span><b id=aDone>0</b> / <b id=aTotal>0</b></span><span class=muted id=aMsg></span></div>
  </div>
  <div id=applyDone hidden></div>
  <pre id=applyErr hidden></pre>
 </div>

</div>
<script>
const $ = s => document.querySelector(s);
const fmt = s => { s=Math.max(0,s|0); return Math.floor(s/60)+':'+String(s%60).padStart(2,'0'); };
let decisions = {}, poller = null;
const key = p => p.source_id + '::' + p.id;
const thumb = p => `/thumb?source_id=${encodeURIComponent(p.source_id)}&id=${encodeURIComponent(p.id)}`;

async function loadConfig(){
  const j = await (await fetch('/api/config')).json();
  if(!j.ok){ $('#cfg').textContent = j.error; return; }
  $('#cfg').innerHTML = j.sources.map(s=>`<div><b>${s.type}</b> — ${s.roots.join(', ')}</div>`).join('') || 'none configured';
}

async function startScan(){
  $('#scanErr').hidden = true; $('#scanDone').hidden = true;
  const r = await fetch('/api/scan',{method:'POST',headers:{'content-type':'application/json'},
    body:JSON.stringify({similarity:+$('#sim').value, refresh:$('#refresh').checked})});
  if(r.status===409){ alert('A job is already running.'); return; }
  $('#scanBtn').disabled = true; $('#scanProg').hidden = false;
  startPolling();
}
async function startApply(){
  if(!confirm('Move the ticked photos into the review folder? Originals are only moved, not deleted.')) return;
  $('#applyErr').hidden = true; $('#applyDone').hidden = true;
  const r = await fetch('/api/apply',{method:'POST'});
  if(r.status===409){ alert('A job is already running.'); return; }
  $('#applyBtn').disabled = true; $('#applyProg').hidden = false;
  startPolling();
}
function startPolling(){ if(poller) return; poller = setInterval(tick, 1000); tick(); }

async function tick(){
  const j = await (await fetch('/api/progress')).json();
  if(j.kind==='scan'){
    $('#pDone').textContent=j.done; $('#pTotal').textContent=j.total;
    $('#pPct').textContent=j.percent; $('#scanFill').style.width=j.percent+'%';
    $('#pEl').textContent=fmt(j.elapsed_s); $('#pEta').textContent=j.done?fmt(j.eta_s):'–';
    $('#pMsg').textContent=j.message;
  }
  if(j.kind==='apply'){
    $('#aDone').textContent=j.done; $('#aTotal').textContent=j.total;
    $('#applyFill').style.width=(j.total?100*j.done/j.total:0)+'%';
    $('#aMsg').textContent=j.message;
  }
  if(j.state==='done' || j.state==='error'){
    clearInterval(poller); poller=null;
    if(j.kind==='scan'){
      $('#scanBtn').disabled=false;
      if(j.state==='error'){ $('#scanErr').hidden=false; $('#scanErr').textContent=j.error; }
      else {
        const s=j.summary||{};
        $('#scanDone').hidden=false;
        $('#scanDone').innerHTML=`<b>${s.groups}</b> duplicate groups &middot; <b>${s.removable}</b> removable photos (~${Math.round((s.reclaim_bytes||0)/1048576)} MB) &middot; <b>${s.flagged}</b> flagged`;
        $('#revBtn').disabled=false; $('#regroupBtn').disabled=false;
      }
    }
    if(j.kind==='apply'){
      $('#applyBtn').disabled=false;
      if(j.state==='error'){ $('#applyErr').hidden=false; $('#applyErr').textContent=j.error; }
      else { $('#applyDone').hidden=false; $('#applyDone').innerHTML=`<b>${(j.summary||{}).moved||0}</b> photos moved. Check the review folder, then delete for good yourself.`; }
    }
  }
}

async function regroup(){
  const btn=$('#regroupBtn'); btn.disabled=true; btn.textContent='re-grouping…';
  const r=await fetch('/api/regroup',{method:'POST',headers:{'content-type':'application/json'},
    body:JSON.stringify({similarity:+$('#sim2').value})});
  const j=await r.json();
  btn.disabled=false; btn.textContent='Re-group at this strictness';
  if(!j.ok){ alert(j.error||'re-group failed'); return; }
  const s=j.summary||{};
  $('#scanDone').hidden=false;
  $('#scanDone').innerHTML=`<b>${s.groups}</b> duplicate groups &middot; <b>${s.removable}</b> removable photos (~${Math.round((s.reclaim_bytes||0)/1048576)} MB) &middot; <b>${s.flagged}</b> flagged`;
  // grouping changed -> clear old picks so nothing stale gets moved
  await fetch('/api/decisions',{method:'POST',headers:{'content-type':'application/json'},body:'{}'});
  decisions={}; $('#applyBtn').disabled=true;
  await loadReview();
}

async function loadReview(){
  const scan = await (await fetch('/api/result')).json();
  decisions = await (await fetch('/api/decisions')).json();
  const box = $('#review'); box.innerHTML='';
  let rm=0;
  scan.groups.forEach(g=>{
    const d=document.createElement('div'); d.className='group';
    d.innerHTML=`<h3>${g.kind} &middot; ${g.members.length} photos</h3>`;
    const row=document.createElement('div'); row.className='row';
    g.members.forEach((p,i)=>{
      if(decisions[key(p)]===undefined) decisions[key(p)] = i===0?'keep':'quarantine';
      row.appendChild(ph(p));
    });
    d.appendChild(row); box.appendChild(d);
  });
  const flagged = scan.photos.filter(p=>p.flags.length && decisions[key(p)]===undefined);
  if(flagged.length){
    const d=document.createElement('div'); d.className='group';
    d.innerHTML='<h3>flagged singles (blurry / dark / blown out)</h3>';
    const row=document.createElement('div'); row.className='row';
    flagged.forEach(p=>{ decisions[key(p)]='keep'; row.appendChild(ph(p)); });
    d.appendChild(row); box.appendChild(d);
  }
  rm = Object.values(decisions).filter(v=>v==='quarantine').length;
  $('#revSummary').textContent = `${rm} ticked for removal`;
  $('#saveBtn').hidden=false;
}
function ph(p){
  const c=document.createElement('div'); c.dataset.k=key(p);
  c.innerHTML=`<img loading=lazy src="${thumb(p)}">
   <div class=n>${p.name}</div><div class=fl>${p.flags.join(', ')}</div>
   <label><input type=checkbox ${decisions[key(p)]==='quarantine'?'checked':''}> remove</label>`;
  c.querySelector('input').onchange=e=>{ decisions[c.dataset.k]=e.target.checked?'quarantine':'keep'; paint(c); updateCount(); };
  paint(c); return c;
}
function paint(c){ c.className='ph '+(decisions[c.dataset.k]==='quarantine'?'remove':'keep'); }
function updateCount(){
  const rm=Object.values(decisions).filter(v=>v==='quarantine').length;
  $('#revSummary').textContent=`${rm} ticked for removal`;
}
async function saveDecisions(){
  const r=await fetch('/api/decisions',{method:'POST',headers:{'content-type':'application/json'},body:JSON.stringify(decisions)});
  const j=await r.json();
  $('#revSummary').textContent=`saved ${j.saved} — now use step 3`;
  $('#applyBtn').disabled=false;
}

loadConfig();
// if a scan already exists from a previous run, let the user jump straight to review
fetch('/api/progress').then(r=>r.json()).then(j=>{ if(j.has_scan){ $('#revBtn').disabled=false; $('#regroupBtn').disabled=false; } if(j.state==='running'){ $('#scanProg').hidden=false; $('#scanBtn').disabled=true; startPolling(); } });
</script></body></html>"""
