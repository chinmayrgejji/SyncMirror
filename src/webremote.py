"""
webremote.py

Optional phone/web remote for SyncMirror. A tiny Flask server runs in a daemon
thread and exposes a PIN-protected JSON API plus a self-contained mobile page.

Threading model (matches the rest of the app): the web thread NEVER touches Tk
widgets or the audio engine directly. Instead it
  - reads a state snapshot the Tk main thread publishes via set_state(), and
  - pushes commands onto a queue the Tk main thread drains.
This keeps everything single-threaded where it matters.
"""

import secrets
import socket
import threading

from flask import Flask, Response, jsonify, request


class WebRemote:
    def __init__(self, command_queue, port: int = 8770):
        self.command_queue = command_queue
        self.port = port
        # 6-digit PIN shown in the desktop app; required on every /api call.
        self.token = f"{secrets.randbelow(1000000):06d}"
        self._state = {"running": False, "source_delay": 0.0,
                       "now_playing": None, "devices": []}
        self._state_lock = threading.Lock()
        # Latest album-art bytes plus a version that bumps on every change so the
        # page only re-fetches the image when the track (art) actually changes.
        self._art_bytes = None
        self._art_mime = "image/jpeg"
        self._art_version = 0
        self._server = None
        self._thread = None

    # -- state shared with the Tk thread --

    def set_state(self, state: dict):
        with self._state_lock:
            self._state = state

    def _get_state(self):
        with self._state_lock:
            return self._state

    def set_artwork(self, data):
        """Publish the current album-art image bytes (or None to clear)."""
        with self._state_lock:
            if data == self._art_bytes:
                return
            self._art_bytes = data
            self._art_version += 1
            if data and data[:8] == b"\x89PNG\r\n\x1a\n":
                self._art_mime = "image/png"
            else:
                self._art_mime = "image/jpeg"

    # -- server lifecycle --

    def _authorized(self) -> bool:
        tok = request.headers.get("X-Auth-Token") or request.args.get("token")
        return bool(tok) and secrets.compare_digest(str(tok), self.token)

    def _build_app(self) -> Flask:
        app = Flask(__name__)

        @app.route("/")
        def index():
            return Response(INDEX_HTML, mimetype="text/html")

        @app.route("/api/ping")
        def ping():
            # Lets the page validate a freshly-entered PIN.
            if not self._authorized():
                return jsonify({"error": "unauthorized"}), 401
            return jsonify({"ok": True})

        @app.route("/api/state")
        def state():
            if not self._authorized():
                return jsonify({"error": "unauthorized"}), 401
            with self._state_lock:
                snap = dict(self._state)
                snap["art_version"] = self._art_version
            return jsonify(snap)

        @app.route("/api/artwork")
        def artwork():
            if not self._authorized():
                return jsonify({"error": "unauthorized"}), 401
            with self._state_lock:
                data, mime = self._art_bytes, self._art_mime
            if not data:
                return Response(status=204)
            return Response(data, mimetype=mime,
                            headers={"Cache-Control": "no-cache"})

        @app.route("/api/command", methods=["POST"])
        def command():
            if not self._authorized():
                return jsonify({"error": "unauthorized"}), 401
            data = request.get_json(force=True, silent=True) or {}
            self.command_queue.put(data)
            return jsonify({"ok": True})

        return app

    def start(self):
        if self._server is not None:
            return
        from werkzeug.serving import make_server
        app = self._build_app()
        self._server = make_server("0.0.0.0", self.port, app, threaded=True)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()

    def stop(self):
        if self._server is not None:
            try:
                self._server.shutdown()
            except Exception:
                pass
            self._server = None
            self._thread = None

    @property
    def running(self) -> bool:
        return self._server is not None

    @staticmethod
    def local_ip() -> str:
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.connect(("8.8.8.8", 80))  # no packets sent; just picks the LAN iface
            ip = s.getsockname()[0]
            s.close()
            return ip
        except Exception:
            return "127.0.0.1"


INDEX_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, maximum-scale=1, user-scalable=no">
<title>SyncMirror Remote</title>
<style>
  :root {
    --bg:#101319; --card:#1a1e27; --hi:#232936; --border:#2a3040;
    --accent:#4da3ff; --good:#3ecf6e; --bad:#e8543b; --warn:#e8b93b;
    --text:#e8ecf4; --dim:#8b93a3; --faint:#5d6577;
  }
  * { box-sizing:border-box; -webkit-tap-highlight-color:transparent; }
  body { margin:0; background:var(--bg); color:var(--text);
         font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif; }
  header { padding:16px 18px 10px; display:flex; align-items:center; justify-content:space-between; }
  header h1 { font-size:19px; margin:0; }
  .status { font-size:12px; font-weight:700; color:var(--dim); }
  .dot { color:var(--bad); }
  .dot.on { color:var(--good); }
  .tabs { display:flex; gap:8px; padding:0 14px 12px; }
  .tab { flex:1; text-align:center; padding:10px; border-radius:10px; background:var(--card);
         border:1px solid var(--border); color:var(--dim); font-weight:700; font-size:14px; }
  .tab.active { background:var(--accent); color:#06121f; border-color:var(--accent); }
  .page { display:none; padding:0 14px 40px; }
  .page.active { display:block; }
  .card { background:var(--card); border:1px solid var(--border); border-radius:12px;
          padding:14px; margin-bottom:12px; }
  .row { display:flex; align-items:center; justify-content:space-between; gap:10px; }
  .label { font-size:11px; font-weight:700; color:var(--faint); letter-spacing:.4px; }
  .name { font-size:15px; font-weight:700; margin-bottom:4px; }
  .val { font-size:12px; font-weight:700; color:var(--text); min-width:52px; text-align:right; }
  input[type=range] { width:100%; height:26px; accent-color:var(--accent); background:transparent; }
  button.btn { border:none; border-radius:10px; padding:12px 16px; font-weight:700; font-size:14px; color:#fff; }
  .primary { background:var(--good); } .danger { background:var(--bad); }
  .ghost { background:transparent; border:1px solid var(--border); color:var(--dim); }
  .pill { padding:6px 10px; border-radius:20px; font-size:11px; font-weight:700; }
  .pill.on { background:rgba(62,207,110,.18); color:var(--good); }
  .pill.off { background:var(--hi); color:var(--faint); }
  .muteb { width:40px; height:34px; border-radius:9px; border:1px solid var(--border);
           background:transparent; color:var(--dim); font-size:16px; }
  .muteb.on { color:var(--bad); border-color:var(--bad); }
  .sw { width:48px; height:28px; border-radius:16px; background:var(--hi); position:relative; border:1px solid var(--border); }
  .sw.on { background:var(--accent); border-color:var(--accent); }
  .sw::after { content:""; position:absolute; top:2px; left:2px; width:22px; height:22px; border-radius:50%;
               background:#fff; transition:left .15s; }
  .sw.on::after { left:22px; }
  .transport { display:flex; align-items:center; justify-content:center; gap:24px; padding:10px 0 2px; }
  .tbtn { width:54px; height:54px; border-radius:50%; border:none; background:var(--hi); color:var(--text); font-size:20px; }
  .tbtn.play { width:64px; height:64px; background:var(--accent); color:#06121f; font-size:24px; }
  .pauseico { display:inline-flex; gap:6px; align-items:center; justify-content:center; }
  .pauseico i { width:6px; height:22px; border-radius:2px; background:currentColor; }
  .bar { height:5px; border-radius:3px; background:var(--hi); overflow:hidden; margin:10px 0 4px; }
  .bar > i { display:block; height:100%; background:var(--accent); width:0%; }
  .times { display:flex; justify-content:space-between; font-size:11px; color:var(--faint); }
  .artstage { position:relative; width:100%; aspect-ratio:16/9; border-radius:12px; overflow:hidden;
              background:var(--hi); margin-bottom:14px; display:flex; align-items:center; justify-content:center; }
  .artstage .bg { position:absolute; inset:0; background-size:cover; background-position:center;
                  filter:blur(26px) brightness(.55) saturate(1.2); transform:scale(1.25); transition:opacity .3s; }
  .artstage img.art { position:relative; max-width:66%; max-height:84%; border-radius:8px; display:none;
                      box-shadow:0 10px 30px rgba(0,0,0,.55); }
  .artstage .ph { position:relative; font-size:46px; color:var(--faint); }
  .artstage.has-art img.art { display:block; }
  .artstage.has-art .ph { display:none; }
  .center { text-align:center; }
  #login { padding:40px 20px; text-align:center; }
  #login input { font-size:22px; letter-spacing:6px; text-align:center; width:180px; padding:12px;
                 border-radius:10px; border:1px solid var(--border); background:var(--card); color:var(--text); }
  .muted-text { color:var(--dim); font-size:13px; }
  .empty { color:var(--faint); font-size:13px; text-align:center; padding:20px; }
</style>
</head>
<body>
<div id="login">
  <h1>SyncMirror Remote</h1>
  <p class="muted-text">Enter the PIN shown in the SyncMirror app.</p>
  <input id="pin" inputmode="numeric" maxlength="6" placeholder="------">
  <p><button class="btn primary" onclick="doLogin()">Connect</button></p>
  <p id="loginerr" style="color:var(--bad); font-size:13px;"></p>
</div>

<div id="app" style="display:none;">
  <header>
    <h1>SyncMirror</h1>
    <div class="status"><span id="dot" class="dot">&#9679;</span> <span id="statustext">--</span></div>
  </header>
  <div class="tabs">
    <div class="tab active" data-tab="playback" onclick="showTab('playback')">Playback</div>
    <div class="tab" data-tab="devices" onclick="showTab('devices')">Devices</div>
  </div>

  <div class="page active" id="page-playback">
    <div class="card">
      <div class="artstage" id="artstage">
        <div class="bg" id="artbg"></div>
        <div class="ph">&#9834;</div>
        <img class="art" id="artimg" alt="" onerror="artError()">
      </div>
      <div class="name" id="np-title">No active track</div>
      <div class="muted-text" id="np-artist"></div>
      <div class="muted-text" id="np-meta" style="color:var(--faint);"></div>
      <div class="bar"><i id="np-bar"></i></div>
      <div class="times"><span id="np-cur">--:--</span><span id="np-dur">--:--</span></div>
      <div class="transport">
        <button class="tbtn" onclick="media('previous')">&#9198;</button>
        <button class="tbtn play" id="playbtn" onclick="media('play_pause')">&#9654;</button>
        <button class="tbtn" onclick="media('next')">&#9197;</button>
      </div>
    </div>
    <div class="card">
      <div class="row">
        <span class="label">SYNC</span>
        <button class="btn primary" id="syncbtn" onclick="toggleSync()">Start Sync</button>
      </div>
    </div>
    <div class="card">
      <div class="row"><span class="label">GLOBAL SOURCE DELAY</span><span class="val" id="src-val">0 ms</span></div>
      <input type="range" id="src" min="-500" max="500" step="5" value="0"
             oninput="srcLabel(this.value)" onchange="setSource(this.value)">
    </div>
  </div>

  <div class="page" id="page-devices">
    <div id="devlist"></div>
    <div class="empty" id="dev-empty" style="display:none;">No output devices detected.</div>
  </div>
</div>

<script>
var token = localStorage.getItem("sm_token") || "";
var activeTab = "playback";
var dragging = {};   // device name -> true while a slider is being dragged

function api(path, opts) {
  opts = opts || {};
  opts.headers = Object.assign({ "X-Auth-Token": token, "Content-Type": "application/json" }, opts.headers || {});
  return fetch(path, opts);
}
function cmd(obj) { return api("/api/command", { method:"POST", body: JSON.stringify(obj) }); }

function doLogin() {
  var pin = document.getElementById("pin").value.trim();
  token = pin;
  api("/api/ping").then(function(r){
    if (r.ok) { localStorage.setItem("sm_token", token); showApp(); }
    else { document.getElementById("loginerr").textContent = "Wrong PIN."; }
  }).catch(function(){ document.getElementById("loginerr").textContent = "Cannot reach server."; });
}
function showApp() {
  document.getElementById("login").style.display = "none";
  document.getElementById("app").style.display = "block";
  poll();
}
function showTab(t) {
  activeTab = t;
  document.querySelectorAll(".tab").forEach(function(e){ e.classList.toggle("active", e.dataset.tab===t); });
  document.querySelectorAll(".page").forEach(function(e){ e.classList.toggle("active", e.id==="page-"+t); });
}
function fmt(s) {
  if (s === null || s === undefined) return "--:--";
  s = Math.max(0, Math.floor(s));
  var m = Math.floor(s/60), ss = s%60;
  return m + ":" + (ss<10?"0":"") + ss;
}
function srcLabel(v){ document.getElementById("src-val").textContent = Math.round(v) + " ms"; }
function artError(){
  var stage = document.getElementById("artstage");
  stage.classList.remove("has-art");
  stage.dataset.av = "";
}
function setSource(v){ cmd({action:"set_source_delay", value: parseFloat(v)}); }
function toggleSync(){ cmd({action:"toggle_sync"}); }
function media(which){ cmd({action:"media", which: which}); }

function render(st) {
  var dot = document.getElementById("dot");
  dot.className = "dot" + (st.running ? " on" : "");
  document.getElementById("statustext").textContent =
      st.running ? (st.devices.filter(function(d){return d.active;}).length + " device(s)") : "Stopped";
  var sb = document.getElementById("syncbtn");
  sb.textContent = st.running ? "Stop Sync" : "Start Sync";
  sb.className = "btn " + (st.running ? "danger" : "primary");

  var np = st.now_playing || {};
  document.getElementById("np-title").textContent = np.title || "No active track";
  document.getElementById("np-artist").textContent = np.artist || "";
  document.getElementById("np-meta").textContent = np.meta || "";
  var cur = np.position, dur = np.duration;
  document.getElementById("np-cur").textContent = fmt(cur);
  document.getElementById("np-dur").textContent = fmt(dur);
  document.getElementById("np-bar").style.width = (dur ? Math.min(100, 100*cur/dur) : 0) + "%";
  // ⏸ while playing (two bars in the button's text color), ▶ while paused.
  document.getElementById("playbtn").innerHTML = np.playing
    ? '<span class="pauseico"><i></i><i></i></span>'
    : "&#9654;";

  // Album art stage: (re)load only when the art version changes; animate bars while playing.
  var stage = document.getElementById("artstage");
  var av = st.art_version || 0;
  if (av && np.title) {
    if (stage.dataset.av !== String(av)) {
      stage.dataset.av = String(av);
      var url = "/api/artwork?token=" + encodeURIComponent(token) + "&v=" + av;
      document.getElementById("artimg").src = url;
      document.getElementById("artbg").style.backgroundImage = "url('" + url + "')";
      stage.classList.add("has-art");
    }
  } else if (stage.dataset.av) {
    stage.dataset.av = "";
    stage.classList.remove("has-art");
    document.getElementById("artimg").removeAttribute("src");
    document.getElementById("artbg").style.backgroundImage = "";
  }
  stage.classList.toggle("playing", !!np.playing);

  var src = document.getElementById("src");
  if (document.activeElement !== src) { src.value = st.source_delay; srcLabel(st.source_delay); }

  var list = document.getElementById("devlist");
  document.getElementById("dev-empty").style.display = st.devices.length ? "none" : "block";
  // Rebuild only when the set of device names changes; otherwise update in place
  // so live polling doesn't fight a slider you're dragging.
  var names = st.devices.map(function(d){return d.name;}).join("|");
  if (list.dataset.names !== names) {
    list.dataset.names = names;
    list.innerHTML = st.devices.map(deviceCard).join("");
  }
  st.devices.forEach(updateDeviceCard);
}

function esc(s){ return (s||"").replace(/[&<>"]/g, function(c){ return {"&":"&amp;","<":"&lt;",">":"&gt;","\"":"&quot;"}[c]; }); }
function id(name){ return "d_" + btoa(unescape(encodeURIComponent(name))).replace(/[^a-zA-Z0-9]/g,""); }

function deviceCard(d) {
  var k = id(d.name);
  return '<div class="card" id="'+k+'">' +
    '<div class="row" style="margin-bottom:10px;">' +
      '<div class="name" style="margin:0;">'+esc(d.name)+'</div>' +
      '<div class="sw" id="'+k+'_sw" onclick="toggleDev(\''+k+'\')"></div>' +
    '</div>' +
    '<div class="row"><span class="label">DELAY</span><span class="val" id="'+k+'_dv">0 ms</span></div>' +
    '<input type="range" min="-500" max="500" step="5" id="'+k+'_d" ' +
      'ontouchstart="dragOn(\''+k+'\')" onmousedown="dragOn(\''+k+'\')" ' +
      'oninput="dLabel(\''+k+'\',this.value)" onchange="setDelay(\''+k+'\',this.value)">' +
    '<div class="row" style="margin-top:6px;"><span class="label">VOLUME</span>' +
      '<div style="display:flex;align-items:center;gap:10px;">' +
      '<span class="val" id="'+k+'_vv">100%</span>' +
      '<button class="muteb" id="'+k+'_m" onclick="toggleMute(\''+k+'\')">&#128266;</button></div></div>' +
    '<input type="range" min="0" max="100" step="1" id="'+k+'_v" ' +
      'ontouchstart="dragOn(\''+k+'\')" onmousedown="dragOn(\''+k+'\')" ' +
      'oninput="vLabel(\''+k+'\',this.value)" onchange="setVol(\''+k+'\',this.value)">' +
  '</div>';
}

var cardData = {};   // k -> device name

function updateDeviceCard(d) {
  var k = id(d.name); cardData[k] = d.name;
  var sw = document.getElementById(k+"_sw"); if (sw) sw.className = "sw" + (d.enabled ? " on":"");
  if (!dragging[k]) {
    var dd = document.getElementById(k+"_d"); if (dd && document.activeElement!==dd){ dd.value = d.delay; document.getElementById(k+"_dv").textContent = Math.round(d.delay)+" ms"; }
    var vv = document.getElementById(k+"_v"); if (vv && document.activeElement!==vv){ vv.value = d.volume; document.getElementById(k+"_vv").textContent = Math.round(d.volume)+"%"; }
  }
  var mb = document.getElementById(k+"_m"); if (mb){ mb.className = "muteb"+(d.muted?" on":""); mb.innerHTML = d.muted ? "&#128263;" : "&#128266;"; }
}

function dragOn(k){ dragging[k]=true; }
function dLabel(k,v){ document.getElementById(k+"_dv").textContent = Math.round(v)+" ms"; }
function vLabel(k,v){ document.getElementById(k+"_vv").textContent = Math.round(v)+"%"; }
function setDelay(k,v){ dragging[k]=false; cmd({action:"set_delay", name:cardData[k], delay:parseFloat(v)}); }
function setVol(k,v){ dragging[k]=false; cmd({action:"set_volume", name:cardData[k], volume:parseFloat(v)}); }
function toggleMute(k){ var mb=document.getElementById(k+"_m"); var on=!mb.classList.contains("on"); cmd({action:"set_muted", name:cardData[k], muted:on}); }
function toggleDev(k){ var sw=document.getElementById(k+"_sw"); var on=!sw.classList.contains("on"); cmd({action:"toggle_device", name:cardData[k], enabled:on}); }

function poll() {
  api("/api/state").then(function(r){
    if (r.status === 401) { localStorage.removeItem("sm_token"); location.reload(); return; }
    return r.json();
  }).then(function(st){ if (st) render(st); }).catch(function(){})
    .finally(function(){ setTimeout(poll, 1000); });
}

// Auto-login when a QR link carries the token (?token=...): validate it, store
// it, strip it from the address bar, then skip the PIN screen.
(function(){
  var qs = new URLSearchParams(location.search);
  var qtok = qs.get("token");
  if (qtok) {
    token = qtok;
    api("/api/ping").then(function(r){
      history.replaceState(null, "", location.pathname);
      if (r.ok) { localStorage.setItem("sm_token", token); showApp(); }
    }).catch(function(){});
    return;
  }
  if (token) { api("/api/ping").then(function(r){ if (r.ok) showApp(); }); }
})();
</script>
</body>
</html>
"""
