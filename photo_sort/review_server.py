"""A tiny local web page for reviewing the last scan.

No framework on the front end: one HTML string, vanilla JS. It shows each
duplicate group (keeper pre-selected, the rest ticked for removal) and the
flagged singles. "Save decisions" writes .photo-sort/decisions.json, which
`photo-sort apply` then acts on.
"""

from __future__ import annotations

import json
from pathlib import Path

from rich.console import Console


def serve(state_dir: Path, host: str, port: int, console: Console) -> None:
    import uvicorn
    from fastapi import FastAPI, Request
    from fastapi.responses import FileResponse, HTMLResponse, JSONResponse

    from photo_sort.store import Store, photo_key

    store = Store(state_dir)
    scan = store.load_scan()
    app = FastAPI()

    @app.get("/", response_class=HTMLResponse)
    def index() -> str:
        return PAGE

    @app.get("/scan.json")
    def scan_json() -> JSONResponse:
        return JSONResponse(scan)

    @app.get("/decisions.json")
    def decisions_json() -> JSONResponse:
        return JSONResponse(store.load_decisions())

    @app.get("/thumb")
    def thumb(source_id: str, id: str):  # noqa: A002 - matches query param name
        p = Path(store.thumb_path_for(photo_key(source_id, id)))
        if not p.exists():
            return JSONResponse({"error": "no thumb"}, status_code=404)
        return FileResponse(p, media_type="image/jpeg")

    @app.post("/decisions.json")
    async def save(request: Request) -> JSONResponse:
        body = await request.json()
        store.save_decisions(body)
        return JSONResponse({"saved": len(body)})

    url = f"http://{host}:{port}"
    console.print(f"Review at [bold]{url}[/]  (Ctrl-C to stop)")
    console.print("Tick = move to review area. Save, then run [bold]photo-sort apply[/].")
    uvicorn.run(app, host=host, port=port, log_level="warning")


PAGE = """<!doctype html><html><head><meta charset=utf-8><title>photo-sort review</title>
<style>
 body{font:14px system-ui;margin:0;background:#f6f6f4;color:#1a1a1a}
 header{position:sticky;top:0;background:#fff;border-bottom:1px solid #ddd;padding:10px 16px;display:flex;gap:12px;align-items:center}
 button{font:inherit;padding:6px 12px;border:1px solid #888;border-radius:6px;background:#fff;cursor:pointer}
 button.primary{background:#1a6;color:#fff;border-color:#1a6}
 .group{margin:16px;padding:12px;background:#fff;border:1px solid #e2e2e2;border-radius:8px}
 .group h3{margin:0 0 8px;font-size:13px;color:#666;font-weight:600}
 .row{display:flex;flex-wrap:wrap;gap:10px}
 .card{width:200px;border:2px solid transparent;border-radius:6px;padding:6px;background:#fafafa}
 .card.remove{border-color:#d33;background:#fff2f2}
 .card.keep{border-color:#1a6}
 .card img{width:100%;height:150px;object-fit:cover;border-radius:4px;background:#eee}
 .card .n{font-size:11px;word-break:break-all;margin:4px 0}
 .card label{font-size:12px;display:flex;gap:4px;align-items:center}
 .flags{color:#b60}
</style></head><body>
<header>
 <strong>photo-sort review</strong>
 <span id=summary></span>
 <span style=flex:1></span>
 <button class=primary onclick=save()>Save decisions</button>
</header>
<div id=app></div>
<script>
let decisions = {};
const key = p => p.source_id + ' ' + p.id;
const thumb = p => `/thumb?source_id=${encodeURIComponent(p.source_id)}&id=${encodeURIComponent(p.id)}`;

async function load(){
  const scan = await (await fetch('/scan.json')).json();
  decisions = await (await fetch('/decisions.json')).json();
  const app = document.getElementById('app');
  scan.groups.forEach(g => {
    const box = document.createElement('div'); box.className='group';
    box.innerHTML = `<h3>${g.kind} &middot; ${g.members.length} photos</h3>`;
    const row = document.createElement('div'); row.className='row';
    g.members.forEach((p,i) => {
      if(decisions[key(p)]===undefined) decisions[key(p)] = i===0 ? 'keep' : 'quarantine';
      row.appendChild(card(p));
    });
    box.appendChild(row); app.appendChild(box);
  });
  const flagged = scan.photos.filter(p => p.flags.length && decisions[key(p)]===undefined);
  if(flagged.length){
    const box=document.createElement('div');box.className='group';
    box.innerHTML='<h3>flagged singles (blurry / dark / blown out)</h3>';
    const row=document.createElement('div');row.className='row';
    flagged.forEach(p=>{decisions[key(p)]='keep';row.appendChild(card(p));});
    box.appendChild(row);app.appendChild(box);
  }
  refresh();
}
function card(p){
  const c=document.createElement('div');c.dataset.k=key(p);
  const checked = decisions[key(p)]==='quarantine' ? 'checked' : '';
  c.innerHTML = `<img loading=lazy src="${thumb(p)}">
   <div class=n>${p.name}</div>
   <div class=flags>${p.flags.join(', ')}</div>
   <label><input type=checkbox ${checked}> remove</label>`;
  c.querySelector('input').onchange = e => {
    decisions[key(p)] = e.target.checked ? 'quarantine' : 'keep';
    paint(c);
  };
  paint(c);
  return c;
}
function paint(c){
  const d = decisions[c.dataset.k];
  c.className = 'card ' + (d==='quarantine'?'remove':'keep');
}
function refresh(){
  const rm = Object.values(decisions).filter(v=>v==='quarantine').length;
  document.getElementById('summary').textContent = `${rm} marked for removal`;
}
async function save(){
  const r = await fetch('/decisions.json',{method:'POST',headers:{'content-type':'application/json'},body:JSON.stringify(decisions)});
  const j = await r.json();
  document.getElementById('summary').textContent = `saved ${j.saved} decisions — now run: photo-sort apply`;
}
document.addEventListener('change',refresh);
load();
</script></body></html>"""
