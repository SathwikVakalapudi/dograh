#!/usr/bin/env python3
"""
Minimal Asterisk trunk dashboard for the Dograh/Unpod setup.

Read-only status (trunk state, live channels, recent CDR) plus a guarded
"place test call" button. Talks to Asterisk ARI on 127.0.0.1:8088 and shells
out to the Asterisk CLI (via docker) for originate + CDR. Bind is LOCALHOST
ONLY — reach it over an SSH tunnel:  ssh -L 8090:localhost:8090 <server>

No third-party deps (stdlib only).
"""
import json, re, base64, subprocess, urllib.request, urllib.error
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ARI_BASE = "http://172.21.0.1:8088/ari"
ARI_USER = "dograh"
ARI_CONF = "/home/ubuntu/dograh/deploy/asterisk/etc/ari.conf"
CONTAINER = "dograh-asterisk-1"
TRUNK = "unpod"                     # PJSIP endpoint name
DIAL_CONTEXT = "test-out"           # dialplan ctx that sets CLI then Dials the trunk
NUM_RE = re.compile(r"^\+?\d{6,15}$")


def ari_pw():
    try:
        for line in open(ARI_CONF):
            if line.strip().startswith("password"):
                return line.split("=", 1)[1].strip()
    except Exception:
        pass
    return ""


def ari_get(path):
    req = urllib.request.Request(ARI_BASE + path)
    tok = base64.b64encode(f"{ARI_USER}:{ari_pw()}".encode()).decode()
    req.add_header("Authorization", "Basic " + tok)
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return json.loads(r.read() or "null")
    except Exception as e:
        return {"error": str(e)}


def cli(cmd):
    try:
        out = subprocess.run(
            ["docker", "exec", CONTAINER, "asterisk", "-rx", cmd],
            capture_output=True, text=True, timeout=15,
        )
        return (out.stdout or "") + (out.stderr or "")
    except Exception as e:
        return f"error: {e}"


def recent_cdr(n=15):
    # Best-effort: read Asterisk CSV CDR if present.
    out = cli_shell(f"tail -n {n} /var/log/asterisk/cdr-csv/Master.csv 2>/dev/null")
    rows = []
    for line in out.strip().splitlines():
        f = [c.strip('"') for c in line.split('","')]
        if len(f) > 9:
            # accountcode,src,dst,dcontext,clid,channel,dstchannel,lastapp,lastdata,start,answer,end,duration,billsec,disposition,...
            rows.append({"src": f[1], "dst": f[2], "start": f[9] if len(f) > 9 else "",
                         "dur": f[12] if len(f) > 12 else "", "disp": f[14] if len(f) > 14 else ""})
    return rows[::-1]


def cli_shell(shell_cmd):
    try:
        out = subprocess.run(
            ["docker", "exec", CONTAINER, "sh", "-c", shell_cmd],
            capture_output=True, text=True, timeout=15,
        )
        return (out.stdout or "") + (out.stderr or "")
    except Exception as e:
        return f"error: {e}"


def status():
    eps = ari_get("/endpoints")
    chans = ari_get("/channels")
    trunk = None
    if isinstance(eps, list):
        for e in eps:
            if e.get("resource") == TRUNK:
                trunk = e
    return {
        "trunk": trunk,
        "channels": chans if isinstance(chans, list) else [],
        "cdr": recent_cdr(),
    }


HTML = """<!doctype html><html><head><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1">
<title>Asterisk Trunk Dashboard</title><style>
:root{--bg:#0f1115;--card:#181b21;--fg:#e6e8eb;--mut:#9aa4b2;--ok:#22c55e;--bad:#ef4444;--acc:#3b82f6;--brd:#262a31}
@media(prefers-color-scheme:light){:root{--bg:#f6f7f9;--card:#fff;--fg:#111;--mut:#667;--brd:#e5e7eb}}
*{box-sizing:border-box}body{margin:0;font:14px/1.5 system-ui,sans-serif;background:var(--bg);color:var(--fg)}
.wrap{max-width:900px;margin:0 auto;padding:20px}
h1{font-size:18px;margin:0 0 4px}.sub{color:var(--mut);margin:0 0 18px;font-size:13px}
.card{background:var(--card);border:1px solid var(--brd);border-radius:10px;padding:16px;margin-bottom:14px}
.card h2{font-size:13px;text-transform:uppercase;letter-spacing:.04em;color:var(--mut);margin:0 0 10px}
.pill{display:inline-block;padding:3px 10px;border-radius:99px;font-weight:600;font-size:13px}
.on{background:rgba(34,197,94,.15);color:var(--ok)}.off{background:rgba(239,68,68,.15);color:var(--bad)}
table{width:100%;border-collapse:collapse;font-size:13px}td,th{text-align:left;padding:6px 8px;border-bottom:1px solid var(--brd)}
th{color:var(--mut);font-weight:600}input,button{font:inherit;border-radius:8px;border:1px solid var(--brd);padding:9px 12px}
input{background:var(--bg);color:var(--fg);width:220px}button{background:var(--acc);color:#fff;border:0;cursor:pointer;font-weight:600}
button:disabled{opacity:.5;cursor:wait}.mut{color:var(--mut)}pre{white-space:pre-wrap;background:var(--bg);border:1px solid var(--brd);border-radius:8px;padding:10px;font-size:12px;max-height:180px;overflow:auto}
.row{display:flex;gap:10px;flex-wrap:wrap;align-items:center}
</style></head><body><div class=wrap>
<h1>Asterisk Trunk Dashboard <span class=mut style="font-weight:400">· Unpod</span></h1>
<p class=sub>Local view of the Asterisk↔Unpod trunk. Auto-refreshes every 3s.</p>

<div class=card><h2>Trunk status</h2><div id=trunk>…</div></div>
<div class=card><h2>Live calls</h2><div id=chans class=mut>…</div></div>
<div class=card><h2>Place test call</h2>
  <div class=row>
    <input id=num placeholder="+919010140931" value="+919010140931">
    <button id=go onclick=call()>Call</button>
    <span class=mut>dials via <code>@unpod</code>, CLI +918071539240</span>
  </div>
  <pre id=out class=mut style="margin-top:12px;display:none"></pre>
</div>
<div class=card><h2>Recent calls (CDR)</h2><div id=cdr class=mut>…</div></div>

<script>
async function tick(){
 try{const s=await (await fetch('/api/status')).json();
  const t=s.trunk; document.getElementById('trunk').innerHTML = t
   ? `<span class="pill ${t.state==='online'?'on':'off'}">${t.state}</span> &nbsp; endpoint <b>${t.resource}</b> · ${t.channel_ids.length} active channel(s)`
   : '<span class="pill off">not found</span>';
  const ch=s.channels||[];
  document.getElementById('chans').innerHTML = ch.length
   ? '<table><tr><th>Caller</th><th>→ To</th><th>State</th></tr>'+ch.map(c=>`<tr><td>${(c.caller&&c.caller.number)||'-'}</td><td>${(c.dialplan&&c.dialplan.exten)||c.name}</td><td>${c.state}</td></tr>`).join('')+'</table>'
   : '<span class=mut>No active calls.</span>';
  const cd=s.cdr||[];
  document.getElementById('cdr').innerHTML = cd.length
   ? '<table><tr><th>Start</th><th>From</th><th>To</th><th>Dur</th><th>Result</th></tr>'+cd.map(r=>`<tr><td>${r.start}</td><td>${r.src}</td><td>${r.dst}</td><td>${r.dur}s</td><td>${r.disp}</td></tr>`).join('')+'</table>'
   : '<span class=mut>No CDR records yet.</span>';
 }catch(e){}
}
async function call(){
 const n=document.getElementById('num').value.trim(); const b=document.getElementById('go'); const o=document.getElementById('out');
 b.disabled=true; o.style.display='block'; o.textContent='Placing call to '+n+' …';
 try{const r=await (await fetch('/api/call',{method:'POST',headers:{'content-type':'application/json'},body:JSON.stringify({number:n})})).json();
   o.textContent=r.ok?('✓ '+(r.message||'originated')+'\\n\\n'+(r.detail||'')):('✗ '+(r.error||'failed'));
 }catch(e){o.textContent='✗ '+e}
 b.disabled=false;
}
tick(); setInterval(tick,3000);
</script></div></body></html>"""


class H(BaseHTTPRequestHandler):
    def _send(self, code, body, ctype="application/json"):
        b = body if isinstance(body, bytes) else body.encode()
        self.send_response(code); self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(b))); self.end_headers(); self.wfile.write(b)

    def log_message(self, *a):  # quiet
        pass

    def do_GET(self):
        if self.path == "/":
            self._send(200, HTML, "text/html; charset=utf-8")
        elif self.path == "/api/status":
            self._send(200, json.dumps(status()))
        else:
            self._send(404, json.dumps({"error": "not found"}))

    def do_POST(self):
        if self.path != "/api/call":
            return self._send(404, json.dumps({"error": "not found"}))
        n = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0)) or 0) or "{}").get("number", "").strip()
        if not NUM_RE.match(n):
            return self._send(400, json.dumps({"error": "invalid number (use +91XXXXXXXXXX)"}))
        # Originate via the test-out dialplan (sets CLI +918071539240, dials PJSIP/<n>@unpod)
        out = cli(f"channel originate Local/{n}@{DIAL_CONTEXT} application Echo")
        ok = "error" not in out.lower() and "failed" not in out.lower()
        self._send(200, json.dumps({"ok": ok, "message": f"originate {n}", "detail": out.strip()[:400]}))


if __name__ == "__main__":
    print("Asterisk dashboard on http://127.0.0.1:8090  (SSH-tunnel to view)")
    ThreadingHTTPServer(("127.0.0.1", 8090), H).serve_forever()
