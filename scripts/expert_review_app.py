#!/usr/bin/env python3
"""Loopback-only web UI for an immutable three-expert review package."""

from __future__ import annotations

import argparse
import hmac
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import secrets
import sys
import threading
import urllib.parse

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from research.expert_review import ExpertReviewStore, build_conflict_report
from research.data import ResearchDataError


_HTML = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Where Papers Go — blinded expert review</title>
<style>
body{font:15px/1.45 system-ui,sans-serif;margin:0;background:#f6f7f9;color:#18202a}main{max-width:1050px;margin:auto;padding:20px}.card{background:white;border:1px solid #d9dee5;border-radius:10px;padding:18px;margin:12px 0}h1,h2{margin:.2em 0 .6em}label{display:block;margin:10px 0 4px;font-weight:600}select,textarea,button{font:inherit;padding:8px}select,textarea{width:100%;box-sizing:border-box}textarea{min-height:90px}button{cursor:pointer;margin-right:8px}.muted{color:#5d6875}.grid{display:grid;grid-template-columns:1fr 1fr;gap:14px}.ok{color:#176b35}.error{color:#a51e2d;white-space:pre-wrap}@media(max-width:750px){.grid{grid-template-columns:1fr}}
</style></head><body><main>
<h1>Blinded expert review</h1><p class="muted">Method identity, original rank, score, and gold label are hidden. Progress is saved locally with a hash-chained audit journal.</p>
<div id="status" class="card">Loading…</div>
<section id="review" hidden>
 <div class="card"><h2 id="aliases"></h2><h3 id="qtitle"></h3><p id="qabstract"></p><div id="constraints" class="muted"></div></div>
 <div class="card"><h2 id="cname"></h2><div id="cmeta" class="muted"></div><h3>Provenance-aware explanation</h3><pre id="explanation"></pre></div>
 <div class="card grid">
  <div><label>Relevance (0–3)</label><select id="relevance"><option value="">Select…</option><option>0</option><option>1</option><option>2</option><option>3</option></select></div>
  <div><label>Submission fit (0–3)</label><select id="submission_fit"><option value="">Select…</option><option>0</option><option>1</option><option>2</option><option>3</option></select></div>
  <div><label>Constraint violation</label><select id="constraint_violation"><option value="">Select…</option><option>none</option><option>minor</option><option>major</option><option>unclear</option></select></div>
  <div><label>Explanation quality</label><select id="explanation_quality"><option value="">Select…</option><option>0</option><option>1</option><option>2</option><option>3</option><option>not_available</option></select></div>
 </div>
 <div class="card"><label>Optional notes</label><textarea id="notes" maxlength="2000"></textarea><label><input id="conflict_phase" type="checkbox"> This is a conflict-review revision</label><button id="save">Save and next</button><button id="previous">Previous</button><button id="conflicts">Review current conflicts</button><div id="message"></div></div>
</section></main>
<script>
const token=new URLSearchParams(location.hash.slice(1)).get('token')||'';let order=[],index=0,annotations={},conflictOrder=null;
async function api(path,options={}){options.headers={...(options.headers||{}),'X-Review-Token':token};const r=await fetch(path,options);const t=await r.text();if(!r.ok)throw new Error(t);return t?JSON.parse(t):{};}
function value(id,v){document.getElementById(id).value=v===undefined?'':String(v)}
async function loadProgress(){const p=await api('/api/progress');order=p.review_ids;annotations=p.annotations;index=Math.max(0,order.findIndex(id=>!annotations[id]));if(index<0)index=0;document.getElementById('status').innerHTML=`Expert <b>${p.expert_id}</b>: <span class="ok">${p.completed}/${p.total}</span> complete, ${p.remaining} remaining.`;document.getElementById('review').hidden=!order.length;await loadItem();}
async function loadItem(){if(!order.length)return;const id=order[Math.min(index,order.length-1)],x=await api('/api/item?review_id='+encodeURIComponent(id));document.getElementById('aliases').textContent=x.query_alias+' / '+x.candidate_alias;document.getElementById('qtitle').textContent=x.query.title;document.getElementById('qabstract').textContent=x.query.abstract;document.getElementById('constraints').textContent='Constraints: '+JSON.stringify(x.query.user_constraints||{});document.getElementById('cname').textContent=x.candidate.name;document.getElementById('cmeta').textContent=JSON.stringify(x.candidate);document.getElementById('explanation').textContent=JSON.stringify(x.explanation,null,2);const a=annotations[id]||{};for(const f of ['relevance','submission_fit','constraint_violation','explanation_quality'])value(f,a[f]);value('notes',a.notes);document.getElementById('conflict_phase').checked=false;document.getElementById('message').textContent='Item '+(index+1)+' / '+order.length;}
document.getElementById('save').onclick=async()=>{try{const id=order[index],body={review_id:id,notes:document.getElementById('notes').value};for(const f of ['relevance','submission_fit']){const v=document.getElementById(f).value;if(v==='')throw new Error('Complete every rating.');body[f]=Number(v)}for(const f of ['constraint_violation','explanation_quality']){let v=document.getElementById(f).value;if(v==='')throw new Error('Complete every rating.');body[f]=/^\d$/.test(v)?Number(v):v}const phase=document.getElementById('conflict_phase').checked?'conflict_review':'initial';const saved=await api('/api/annotation?phase='+phase,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});annotations[id]=saved.annotation;index=Math.min(index+1,order.length-1);await loadProgress();}catch(e){document.getElementById('message').className='error';document.getElementById('message').textContent=e.message;}};
document.getElementById('previous').onclick=async()=>{index=Math.max(0,index-1);await loadItem()};document.getElementById('conflicts').onclick=async()=>{try{const c=await api('/api/conflicts');conflictOrder=c.conflicts.map(x=>x.review_id);if(!conflictOrder.length)throw new Error('No complete-triplet conflicts are currently available.');order=conflictOrder;index=0;document.getElementById('conflict_phase').checked=true;await loadItem()}catch(e){document.getElementById('message').className='error';document.getElementById('message').textContent=e.message;}};
loadProgress().catch(e=>{document.getElementById('status').className='card error';document.getElementById('status').textContent=e.message});
</script></body></html>"""


class ReviewServer(ThreadingHTTPServer):
    def __init__(self, address, handler, *, store, token, package_dir, state_dir):
        super().__init__(address, handler)
        self.store = store
        self.access_token = token
        self.package_dir = package_dir
        self.state_dir = state_dir
        self.write_lock = threading.Lock()


class Handler(BaseHTTPRequestHandler):
    server_version = "where-papers-go-expert-review/1.0"

    def _headers(self, status: int, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy", "default-src 'self'; style-src 'unsafe-inline'; script-src 'unsafe-inline'; connect-src 'self'")
        self.end_headers()

    def _json(self, payload, status=HTTPStatus.OK):
        body=json.dumps(payload,ensure_ascii=False,sort_keys=True).encode("utf-8")
        self._headers(status,"application/json; charset=utf-8")
        self.wfile.write(body)

    def _authorized(self) -> bool:
        supplied=self.headers.get("X-Review-Token","")
        return hmac.compare_digest(supplied,self.server.access_token)

    def _require_auth(self) -> bool:
        if self._authorized(): return True
        self._json({"error":"unauthorized"},HTTPStatus.UNAUTHORIZED);return False

    def do_GET(self):
        parsed=urllib.parse.urlparse(self.path)
        try:
            if parsed.path=="/":
                self._headers(HTTPStatus.OK,"text/html; charset=utf-8");self.wfile.write(_HTML.encode("utf-8"));return
            if not self._require_auth(): return
            if parsed.path=="/api/progress":
                progress=self.server.store.progress();snapshot=self.server.store.snapshot();progress.update({"review_ids":list(self.server.store.review_ids),"annotations":snapshot});self._json(progress);return
            if parsed.path=="/api/item":
                review_id=urllib.parse.parse_qs(parsed.query).get("review_id",[""])[0]
                item=self.server.store.items.get(review_id)
                if item is None:self._json({"error":"unknown review_id"},HTTPStatus.NOT_FOUND);return
                self._json(item);return
            if parsed.path=="/api/conflicts":
                self._json(build_conflict_report(self.server.package_dir,self.server.state_dir));return
            self._json({"error":"not found"},HTTPStatus.NOT_FOUND)
        except (OSError,ValueError,ResearchDataError,json.JSONDecodeError) as exc:self._json({"error":str(exc)},HTTPStatus.BAD_REQUEST)

    def do_POST(self):
        parsed=urllib.parse.urlparse(self.path)
        if not self._require_auth(): return
        if parsed.path!="/api/annotation":self._json({"error":"not found"},HTTPStatus.NOT_FOUND);return
        try:
            size=int(self.headers.get("Content-Length","0"))
            if size<2 or size>65536:raise ResearchDataError("invalid request size")
            payload=json.loads(self.rfile.read(size))
            if not isinstance(payload,dict):raise ResearchDataError("annotation must be an object")
            phase=urllib.parse.parse_qs(parsed.query).get("phase",["initial"])[0]
            with self.server.write_lock:annotation=self.server.store.save(payload,phase=phase)
            self._json({"saved":True,"annotation":annotation})
        except (OSError,ValueError,ResearchDataError,json.JSONDecodeError) as exc:self._json({"error":str(exc)},HTTPStatus.BAD_REQUEST)

    def log_message(self, fmt, *args):
        sys.stderr.write("expert-review " + (fmt % args) + "\n")


def build_parser() -> argparse.ArgumentParser:
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--package-dir",type=Path,required=True)
    parser.add_argument("--state-dir",type=Path,required=True)
    parser.add_argument("--expert-id",required=True)
    parser.add_argument("--host",default="127.0.0.1")
    parser.add_argument("--port",type=int,default=8002)
    parser.add_argument("--allow-lan",action="store_true")
    parser.add_argument("--access-token",default="")
    return parser


def main(argv=None) -> int:
    args=build_parser().parse_args(argv)
    if args.host not in {"127.0.0.1","::1","localhost"} and not args.allow_lan:
        print("refusing non-loopback bind without --allow-lan",file=sys.stderr);return 2
    token=args.access_token or secrets.token_urlsafe(32)
    if len(token)<24:print("access token must contain at least 24 characters",file=sys.stderr);return 2
    try:store=ExpertReviewStore(args.package_dir,args.state_dir,args.expert_id)
    except (OSError,ValueError,ResearchDataError) as exc:print(f"expert review error: {exc}",file=sys.stderr);return 2
    server=ReviewServer((args.host,args.port),Handler,store=store,token=token,package_dir=args.package_dir.resolve(),state_dir=args.state_dir.resolve())
    print(f"Open http://{args.host}:{args.port}/#token={token}",flush=True)
    try:server.serve_forever()
    except KeyboardInterrupt:pass
    finally:server.server_close()
    return 0


if __name__=="__main__":raise SystemExit(main())
