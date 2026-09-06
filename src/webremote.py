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

import json
import secrets
import socket
import threading

from flask import Flask, Response, jsonify, request

# Commands the page is allowed to send; anything else is rejected so the API
# can confirm what it actually applied instead of blindly returning ok.
KNOWN_ACTIONS = {
    "toggle_sync", "start", "stop", "set_source_delay", "media",
    "set_delay", "set_volume", "set_muted", "toggle_device",
    "set_eq_enabled", "set_eq_band", "set_eq_preset",
}


class WebRemote:
    def __init__(self, command_queue, port: int = 8770):
        self.command_queue = command_queue
        self.port = port
        # 6-digit PIN shown in the desktop app; required on every /api call.
        self.token = f"{secrets.randbelow(1000000):06d}"
        self._state = {"running": False, "source_delay": 0.0,
                       "now_playing": None, "devices": [],
                       "eq": {"enabled": False, "gains": [], "bands": []}}
        self._state_lock = threading.Lock()
        # A Condition over the same lock lets SSE clients block until the state
        # actually changes instead of polling on a timer.
        self._state_cond = threading.Condition(self._state_lock)
        self._state_version = 0
        # Latest album-art bytes plus a version that bumps on every change so the
        # page only re-fetches the image when the track (art) actually changes.
        self._art_bytes = None
        self._art_mime = "image/jpeg"
        self._art_version = 0
        self._server = None
        self._thread = None

    # -- state shared with the Tk thread --

    def set_state(self, state: dict):
        with self._state_cond:
            self._state = state
            self._state_version += 1
            self._state_cond.notify_all()

    def _get_state(self):
        with self._state_lock:
            return self._state

    def _snapshot(self):
        """State plus the current art version, under the lock."""
        with self._state_lock:
            snap = dict(self._state)
            snap["art_version"] = self._art_version
            return snap

    def set_artwork(self, data):
        """Publish the current album-art image bytes (or None to clear)."""
        with self._state_cond:
            if data == self._art_bytes:
                return
            self._art_bytes = data
            self._art_version += 1
            self._state_version += 1
            if data and data[:8] == b"\x89PNG\r\n\x1a\n":
                self._art_mime = "image/png"
            else:
                self._art_mime = "image/jpeg"
            self._state_cond.notify_all()

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
            return jsonify(self._snapshot())

        @app.route("/api/events")
        def events():
            # Server-Sent Events: push a fresh snapshot the instant state changes
            # so transport taps and slider moves reflect without a poll delay.
            if not self._authorized():
                return jsonify({"error": "unauthorized"}), 401

            def stream():
                last = -1
                while True:
                    with self._state_cond:
                        if self._state_version == last:
                            self._state_cond.wait(timeout=20.0)
                        changed = self._state_version != last
                        last = self._state_version
                        snap = dict(self._state)
                        snap["art_version"] = self._art_version
                    if changed:
                        yield "event: state\ndata: " + json.dumps(snap) + "\n\n"
                    else:
                        yield ": keep-alive\n\n"  # heartbeat so proxies/clients hold the line

            return Response(stream(), mimetype="text/event-stream",
                            headers={"Cache-Control": "no-cache",
                                     "X-Accel-Buffering": "no",
                                     "Connection": "keep-alive"})

        @app.route("/manifest.webmanifest")
        def manifest():
            return Response(MANIFEST_JSON, mimetype="application/manifest+json")

        @app.route("/icon.svg")
        def icon():
            return Response(ICON_SVG, mimetype="image/svg+xml",
                            headers={"Cache-Control": "max-age=86400"})

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
            action = data.get("action")
            if action not in KNOWN_ACTIONS:
                return jsonify({"ok": False, "error": "unknown_action",
                                "action": action}), 400
            self.command_queue.put(data)
            # The command applies on the Tk thread; return the current snapshot so
            # the page can reconcile its optimistic update (SSE follows shortly).
            return jsonify({"ok": True, "state": self._snapshot()})

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


ICON_SVG = (
    '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 192 192">'
    '<rect width="192" height="192" rx="42" fill="#101319"/>'
    '<circle cx="96" cy="96" r="56" fill="none" stroke="#4da3ff" stroke-width="11"/>'
    '<circle cx="96" cy="96" r="31" fill="none" stroke="#3ecf6e" stroke-width="11"/>'
    '<circle cx="96" cy="96" r="11" fill="#e8ecf4"/>'
    '</svg>'
)

MANIFEST_JSON = json.dumps({
    "name": "SyncMirror Remote",
    "short_name": "SyncMirror",
    "description": "Remote control for SyncMirror",
    "start_url": "/",
    "scope": "/",
    "display": "standalone",
    "orientation": "portrait",
    "background_color": "#101319",
    "theme_color": "#101319",
    "icons": [
        {"src": "/icon.svg", "sizes": "any", "type": "image/svg+xml",
         "purpose": "any maskable"},
    ],
})


INDEX_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, maximum-scale=1, user-scalable=no, viewport-fit=cover">
<meta name="theme-color" content="#101319">
<meta name="mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">
<meta name="apple-mobile-web-app-title" content="SyncMirror">
<link rel="manifest" href="/manifest.webmanifest">
<link rel="icon" href="/icon.svg">
<link rel="apple-touch-icon" href="/icon.svg">
<title>SyncMirror Remote</title>
<style>
  :root {
    --bg:#101319; --card:#1a1e27; --hi:#232936; --border:#2a3040;
    --accent:#4da3ff; --good:#3ecf6e; --bad:#e8543b; --warn:#e8b93b;
    --text:#e8ecf4; --dim:#8b93a3; --faint:#5d6577;
  }
  * { box-sizing:border-box; -webkit-tap-highlight-color:transparent; }
  body { margin:0; background:var(--bg); color:var(--text);
         font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif;
         padding:env(safe-area-inset-top) env(safe-area-inset-right)
                 env(safe-area-inset-bottom) env(safe-area-inset-left); }
  header { padding:16px 18px 10px; display:flex; align-items:center; justify-content:space-between; gap:10px; }
  header h1 { font-size:19px; margin:0; }
  .hstatus { display:flex; align-items:center; gap:8px; }
  .status { font-size:12px; font-weight:700; color:var(--dim); }
  .dot { color:var(--bad); }
  .dot.on { color:var(--good); }
  .repill { background:rgba(232,185,59,.16); color:var(--warn); padding:4px 10px;
            border-radius:20px; font-size:11px; font-weight:700; display:none; }
  .repill.show { display:inline-block; }
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
  .srow { display:flex; align-items:center; gap:10px; margin-top:2px; }
  .srow input[type=range] { flex:1; }
  .nudge { width:38px; height:38px; flex:0 0 auto; border-radius:10px; border:1px solid var(--border);
           background:transparent; color:var(--dim); font-size:20px; font-weight:700; line-height:1; }
  .nudge:active { background:var(--hi); }
  input[type=range] { -webkit-appearance:none; appearance:none; width:100%; height:30px;
                      background:
                        linear-gradient(var(--accent),var(--accent)) no-repeat,
                        linear-gradient(var(--hi),var(--hi)) no-repeat;
                      background-size: var(--fill,0%) 6px, 100% 6px;
                      background-position: left center, left center;
                      border-radius:3px; }
  input[type=range]::-webkit-slider-runnable-track { height:6px; background:transparent; border-radius:3px; }
  input[type=range]::-webkit-slider-thumb { -webkit-appearance:none; margin-top:-12px;
                      width:28px; height:28px; border-radius:50%; background:var(--accent);
                      border:3px solid var(--bg); box-shadow:0 2px 7px rgba(0,0,0,.45); }
  input[type=range]::-moz-range-track { height:6px; background:var(--hi); border-radius:3px; }
  input[type=range]::-moz-range-progress { height:6px; background:var(--accent); border-radius:3px; }
  input[type=range]::-moz-range-thumb { width:26px; height:26px; border:3px solid var(--bg);
                      border-radius:50%; background:var(--accent); }
  svg.ic { width:1em; height:1em; display:block; fill:currentColor; }
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
  .tbtn { width:54px; height:54px; border-radius:50%; border:none; background:var(--hi); color:var(--text);
          font-size:22px; display:flex; align-items:center; justify-content:center; }
  .tbtn.play { width:64px; height:64px; background:var(--accent); color:#06121f; font-size:26px; }
  .muteb svg.ic, .tbtn svg.ic { width:1em; height:1em; }
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
  .presets { display:flex; flex-wrap:wrap; gap:8px; }
  .chip { padding:8px 12px; border-radius:20px; border:1px solid var(--border);
          background:transparent; color:var(--dim); font-size:13px; font-weight:700; }
  .chip.on { background:var(--accent); color:#06121f; border-color:var(--accent); }
  .eqrow { display:flex; align-items:center; gap:10px; margin:6px 0; }
  .eqrow .bl { width:40px; font-size:12px; color:var(--dim); font-weight:700; }
  .eqrow .bv { width:40px; text-align:right; font-size:12px; color:var(--text); font-weight:700; }
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
    <div class="hstatus">
      <span class="repill" id="reconnect">reconnecting…</span>
      <div class="status"><span id="dot" class="dot">&#9679;</span> <span id="statustext">--</span></div>
    </div>
  </header>
  <div class="tabs">
    <div class="tab active" data-tab="playback" onclick="showTab('playback')">Playback</div>
    <div class="tab" data-tab="devices" onclick="showTab('devices')">Devices</div>
    <div class="tab" data-tab="eq" onclick="showTab('eq')">EQ</div>
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
        <button class="tbtn" onclick="media('previous')" aria-label="Previous"></button>
        <button class="tbtn play" id="playbtn" onclick="media('play_pause')" aria-label="Play/Pause"></button>
        <button class="tbtn" onclick="media('next')" aria-label="Next"></button>
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
      <div class="srow">
        <button class="nudge" onclick="nudgeSrc(-5)" aria-label="Decrease">&#8722;</button>
        <input type="range" id="src" min="-500" max="500" step="5" value="0"
               oninput="srcInput(this.value)" onchange="setSource(this.value)">
        <button class="nudge" onclick="nudgeSrc(5)" aria-label="Increase">+</button>
      </div>
    </div>
  </div>

  <div class="page" id="page-devices">
    <div id="devlist"></div>
    <div class="empty" id="dev-empty" style="display:none;">No output devices detected.</div>
  </div>

  <div class="page" id="page-eq">
    <div class="card">
      <div class="row">
        <span class="label">EQUALIZER</span>
        <div class="sw" id="eq_sw" onclick="toggleEq()"></div>
      </div>
      <div class="muted-text" style="margin-top:6px;">One global EQ, applied to every device at once.</div>
    </div>
    <div class="card">
      <div class="label" style="margin-bottom:10px;">PRESETS</div>
      <div id="eq-presets" class="presets"></div>
    </div>
    <div class="card" id="eq-bands"></div>
  </div>
</div>

<script>
var token = localStorage.getItem("sm_token") || "";
var activeTab = "playback";
var dragging = {};   // device name -> true while a slider is being dragged
var np_state = { pos:null, dur:null, playing:false, anchor:0 };  // for local progress ticking

// Inline SVG icon set (fill/stroke follow the button's currentColor) so glyphs
// render identically on every phone instead of platform emoji/entities.
var ICON = {
  prev:'<svg class="ic" viewBox="0 0 24 24"><path d="M7 6h2.2v12H7zM20 6l-11 6 11 6z"/></svg>',
  next:'<svg class="ic" viewBox="0 0 24 24"><path d="M14.8 6H17v12h-2.2zM4 6l11 6-11 6z"/></svg>',
  play:'<svg class="ic" viewBox="0 0 24 24"><path d="M8 5v14l11-7z"/></svg>',
  pause:'<svg class="ic" viewBox="0 0 24 24"><rect x="6.5" y="5" width="4" height="14" rx="1"/><rect x="13.5" y="5" width="4" height="14" rx="1"/></svg>',
  spk:'<svg class="ic" viewBox="0 0 24 24"><path d="M4 9v6h4l5 4V5L8 9z"/><path d="M16 8.5a4 4 0 010 7" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round"/></svg>',
  mute:'<svg class="ic" viewBox="0 0 24 24"><path d="M4 9v6h4l5 4V5L8 9z"/><path d="M17 9l4 6M21 9l-4 6" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round"/></svg>'
};

function clamp(v, lo, hi){ return Math.max(lo, Math.min(hi, v)); }
function haptic(ms){ if (navigator.vibrate) { try { navigator.vibrate(ms || 12); } catch(e){} } }
function fillTrack(el){
  var mn = parseFloat(el.min), mx = parseFloat(el.max), v = parseFloat(el.value);
  el.style.setProperty("--fill", (mx > mn ? (v - mn) / (mx - mn) * 100 : 0) + "%");
}

// Keep the phone awake while the remote is open so the screen doesn't lock mid-session.
var wakeLock = null;
function requestWake(){
  try {
    if ("wakeLock" in navigator) {
      navigator.wakeLock.request("screen").then(function(w){ wakeLock = w; }).catch(function(){});
    }
  } catch(e){}
}
document.addEventListener("visibilitychange", function(){
  if (document.visibilityState === "visible") requestWake();
});

function setOnline(){ document.getElementById("reconnect").classList.remove("show"); }
function setOffline(){ document.getElementById("reconnect").classList.add("show"); }

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
  connect();
  requestWake();
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
function srcInput(v){ srcLabel(v); fillTrack(document.getElementById("src")); }
function nudgeSrc(delta){
  var el = document.getElementById("src");
  var v = clamp(parseFloat(el.value || 0) + delta, -500, 500);
  el.value = v; srcInput(v); setSource(v); haptic(8);
}
function artError(){
  var stage = document.getElementById("artstage");
  stage.classList.remove("has-art");
  stage.dataset.av = "";
}
function setSource(v){ cmd({action:"set_source_delay", value: parseFloat(v)}); }
function toggleSync(){
  // Optimistic: flip the button now, let the next state event confirm.
  var running = document.getElementById("syncbtn").classList.contains("danger");
  paintSync(!running);
  haptic();
  cmd({action:"toggle_sync"});
}
function paintSync(running){
  var sb = document.getElementById("syncbtn");
  sb.textContent = running ? "Stop Sync" : "Start Sync";
  sb.className = "btn " + (running ? "danger" : "primary");
}
function paintPlay(playing){
  document.getElementById("playbtn").innerHTML = playing ? ICON.pause : ICON.play;
}
function media(which){
  haptic();
  if (which === "play_pause") { np_state.playing = !np_state.playing; paintPlay(np_state.playing); }
  cmd({action:"media", which: which});
}

function render(st) {
  var dot = document.getElementById("dot");
  dot.className = "dot" + (st.running ? " on" : "");
  document.getElementById("statustext").textContent =
      st.running ? (st.devices.filter(function(d){return d.active;}).length + " device(s)") : "Stopped";
  paintSync(st.running);

  var tp = document.querySelectorAll(".transport .tbtn");
  if (tp.length === 3 && !tp[0].innerHTML) { tp[0].innerHTML = ICON.prev; tp[2].innerHTML = ICON.next; }

  var np = st.now_playing || {};
  document.getElementById("np-title").textContent = np.title || "No active track";
  document.getElementById("np-artist").textContent = np.artist || "";
  document.getElementById("np-meta").textContent = np.meta || "";
  var cur = np.position, dur = np.duration;
  document.getElementById("np-dur").textContent = fmt(dur);
  // Snapshot for local ticking so the progress bar advances smoothly between events.
  np_state.pos = (cur === null || cur === undefined) ? null : cur;
  np_state.dur = dur || null;
  np_state.playing = !!np.playing;
  np_state.anchor = Date.now() / 1000;
  document.getElementById("np-cur").textContent = fmt(cur);
  document.getElementById("np-bar").style.width = (dur ? Math.min(100, 100*cur/dur) : 0) + "%";
  paintPlay(np_state.playing);

  // Album art stage: (re)load only when the art version changes.
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
  if (document.activeElement !== src) { src.value = st.source_delay; srcLabel(st.source_delay); fillTrack(src); }

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
  renderEq(st.eq);
}

// Advance the progress bar locally between server events so it never stutters.
function tickProgress(){
  if (!(np_state.playing && np_state.pos !== null && np_state.dur)) return;
  var p = Math.min(np_state.dur, np_state.pos + (Date.now() / 1000 - np_state.anchor));
  document.getElementById("np-cur").textContent = fmt(p);
  document.getElementById("np-bar").style.width = (100 * p / np_state.dur) + "%";
}
setInterval(tickProgress, 500);

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
    '<div class="srow">' +
      '<button class="nudge" onclick="nudgeDelay(\''+k+'\',-5)">&#8722;</button>' +
      '<input type="range" min="-500" max="500" step="5" id="'+k+'_d" ' +
        'ontouchstart="dragOn(\''+k+'\')" onmousedown="dragOn(\''+k+'\')" ' +
        'oninput="dLabel(\''+k+'\',this.value)" onchange="setDelay(\''+k+'\',this.value)">' +
      '<button class="nudge" onclick="nudgeDelay(\''+k+'\',5)">+</button>' +
    '</div>' +
    '<div class="row" style="margin-top:6px;"><span class="label">VOLUME</span>' +
      '<div style="display:flex;align-items:center;gap:10px;">' +
      '<span class="val" id="'+k+'_vv">100%</span>' +
      '<button class="muteb" id="'+k+'_m" onclick="toggleMute(\''+k+'\')">'+ICON.spk+'</button></div></div>' +
    '<div class="srow">' +
      '<button class="nudge" onclick="nudgeVol(\''+k+'\',-5)">&#8722;</button>' +
      '<input type="range" min="0" max="100" step="1" id="'+k+'_v" ' +
        'ontouchstart="dragOn(\''+k+'\')" onmousedown="dragOn(\''+k+'\')" ' +
        'oninput="vLabel(\''+k+'\',this.value)" onchange="setVol(\''+k+'\',this.value)">' +
      '<button class="nudge" onclick="nudgeVol(\''+k+'\',5)">+</button>' +
    '</div>' +
  '</div>';
}

var cardData = {};   // k -> device name

function updateDeviceCard(d) {
  var k = id(d.name); cardData[k] = d.name;
  var sw = document.getElementById(k+"_sw"); if (sw) sw.className = "sw" + (d.enabled ? " on":"");
  if (!dragging[k]) {
    var dd = document.getElementById(k+"_d"); if (dd && document.activeElement!==dd){ dd.value = d.delay; document.getElementById(k+"_dv").textContent = Math.round(d.delay)+" ms"; fillTrack(dd); }
    var vv = document.getElementById(k+"_v"); if (vv && document.activeElement!==vv){ vv.value = d.volume; document.getElementById(k+"_vv").textContent = Math.round(d.volume)+"%"; fillTrack(vv); }
  }
  var mb = document.getElementById(k+"_m"); if (mb){ mb.className = "muteb"+(d.muted?" on":""); mb.innerHTML = d.muted ? ICON.mute : ICON.spk; }
}

function dragOn(k){ dragging[k]=true; }
function dLabel(k,v){ document.getElementById(k+"_dv").textContent = Math.round(v)+" ms"; fillTrack(document.getElementById(k+"_d")); }
function vLabel(k,v){ document.getElementById(k+"_vv").textContent = Math.round(v)+"%"; fillTrack(document.getElementById(k+"_v")); }
function setDelay(k,v){ dragging[k]=false; cmd({action:"set_delay", name:cardData[k], delay:parseFloat(v)}); }
function setVol(k,v){ dragging[k]=false; cmd({action:"set_volume", name:cardData[k], volume:parseFloat(v)}); }
function nudgeDelay(k,delta){
  var el = document.getElementById(k+"_d");
  var v = clamp(parseFloat(el.value || 0) + delta, -500, 500);
  el.value = v; dLabel(k, v); setDelay(k, v); haptic(8);
}
function nudgeVol(k,delta){
  var el = document.getElementById(k+"_v");
  var v = clamp(parseFloat(el.value || 0) + delta, 0, 100);
  el.value = v; vLabel(k, v); setVol(k, v); haptic(8);
}
function toggleMute(k){
  var mb=document.getElementById(k+"_m"); var on=!mb.classList.contains("on");
  mb.className = "muteb" + (on ? " on" : ""); mb.innerHTML = on ? ICON.mute : ICON.spk;  // optimistic
  haptic();
  cmd({action:"set_muted", name:cardData[k], muted:on});
}
function toggleDev(k){
  var sw=document.getElementById(k+"_sw"); var on=!sw.classList.contains("on");
  sw.className = "sw" + (on ? " on" : "");  // optimistic
  haptic();
  cmd({action:"toggle_device", name:cardData[k], enabled:on});
}

// ---- Equalizer (global; one curve shared by every device) ----
var EQ_PRESETS = {
  "Flat":[0,0,0,0,0,0,0,0,0,0],
  "Bass Boost":[7,6,5,3,1,0,0,0,0,0],
  "Treble Boost":[0,0,0,0,0,1,3,5,6,7],
  "Vocal":[-3,-2,-1,2,4,4,3,1,0,-1],
  "Rock":[5,4,2,-1,-1,0,2,4,5,5],
  "Loudness":[6,5,3,0,-1,-1,1,3,5,6]
};
var EQ_ORDER = ["Flat","Bass Boost","Treble Boost","Vocal","Rock","Loudness"];
function fmtHz(h){ return h>=1000 ? (h/1000)+"k" : (""+h); }
function fmtGain(v){ v=Math.round(v); return (v>0?"+":"")+v; }
function detectPreset(g){
  for (var i=0;i<EQ_ORDER.length;i++){ var n=EQ_ORDER[i], p=EQ_PRESETS[n], ok=true;
    for (var j=0;j<p.length;j++){ if (Math.abs((g[j]||0)-p[j])>=0.5){ ok=false; break; } }
    if (ok) return n; }
  return "Custom";
}
function renderEq(eq){
  if (!eq) return;
  var sw=document.getElementById("eq_sw"); if (sw) sw.className="sw"+(eq.enabled?" on":"");
  var bands=eq.bands||[], gains=eq.gains||[];
  var presWrap=document.getElementById("eq-presets");
  if (presWrap && !presWrap.dataset.built){ presWrap.dataset.built="1";
    presWrap.innerHTML = EQ_ORDER.map(function(n){
      return '<button class="chip" data-p="'+esc(n)+'" onclick="setEqPreset(\''+esc(n)+'\')">'+esc(n)+'</button>';
    }).join("");
  }
  var wrap=document.getElementById("eq-bands");
  if (wrap && wrap.dataset.bands !== bands.join(",")){
    wrap.dataset.bands = bands.join(",");
    wrap.innerHTML = bands.map(function(h,i){
      return '<div class="eqrow">'+
        '<span class="bl">'+fmtHz(h)+'</span>'+
        '<input type="range" min="-12" max="12" step="1" id="eqb_'+i+'" '+
          'ontouchstart="dragOn(\'eq_'+i+'\')" onmousedown="dragOn(\'eq_'+i+'\')" '+
          'oninput="eqLabel('+i+',this.value)" onchange="setEqBand('+i+',this.value)">'+
        '<span class="bv" id="eqv_'+i+'">0</span>'+
      '</div>';
    }).join("");
  }
  gains.forEach(function(g,i){
    if (dragging["eq_"+i]) return;
    var el=document.getElementById("eqb_"+i);
    if (el && document.activeElement!==el){ el.value=g;
      document.getElementById("eqv_"+i).textContent=fmtGain(g); fillTrack(el); }
  });
  var active=detectPreset(gains);
  if (presWrap){ presWrap.querySelectorAll(".chip").forEach(function(c){
    c.classList.toggle("on", c.dataset.p===active); }); }
}
function toggleEq(){
  var sw=document.getElementById("eq_sw"); var on=!sw.classList.contains("on");
  sw.className="sw"+(on?" on":""); haptic();  // optimistic
  cmd({action:"set_eq_enabled", enabled:on});
}
function eqLabel(i,v){ document.getElementById("eqv_"+i).textContent=fmtGain(v);
  fillTrack(document.getElementById("eqb_"+i)); }
function setEqBand(i,v){ dragging["eq_"+i]=false;
  cmd({action:"set_eq_band", index:i, gain:parseFloat(v)}); }
function setEqPreset(n){ haptic(); cmd({action:"set_eq_preset", preset:n}); }

// Prefer Server-Sent Events (instant push); fall back to polling if the browser
// lacks EventSource or the stream can't be established.
var usingSSE = false;
function connect() {
  if (window.EventSource) {
    try {
      var es = new EventSource("/api/events?token=" + encodeURIComponent(token));
      es.addEventListener("state", function(ev){
        usingSSE = true; setOnline();
        try { render(JSON.parse(ev.data)); } catch(e){}
      });
      es.onopen = function(){ setOnline(); };
      es.onerror = function(){
        setOffline();
        // EventSource retries on its own; if it never connected, fall back to polling.
        if (!usingSSE) { try { es.close(); } catch(e){} poll(); }
      };
      return;
    } catch(e){}
  }
  poll();
}

function poll() {
  api("/api/state").then(function(r){
    if (r.status === 401) { localStorage.removeItem("sm_token"); location.reload(); return; }
    setOnline();
    return r.json();
  }).then(function(st){ if (st) render(st); })
    .catch(function(){ setOffline(); })
    .finally(function(){ if (!usingSSE) setTimeout(poll, 1000); });
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
