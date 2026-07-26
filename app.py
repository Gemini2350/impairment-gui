#!/usr/bin/env python3
"""Web-GUI: Netzwerk-Impairment (Packetloss/Jitter) auf LEG A und LEG B schalten.

Sendet  PUT <host>/api/p_cap/instances/1/mode_command  mit Body
[mode, [min_delay_ns, jitter, packetloss]]   z.B.  [1,[1000000,0,0.0007]]
"""

import collections
import json
import os
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request

from flask import Flask, jsonify, redirect, render_template_string, request

DATA_DIR = os.environ.get("DATA_DIR") or os.path.dirname(os.path.abspath(__file__))
CONFIG_FILE = os.path.join(DATA_DIR, "config.json")
PING_INTERVAL = 3          # Sekunden zwischen Ping-Runden
HTTP_TIMEOUT = 5           # Timeout fuer PUT ans Geraet
LEGS = ("A", "B")
FUNCS = ("packetloss", "jitter")

DEFAULT_CONFIG = {
    "hosts": {"A": "192.168.1.10", "B": "192.168.1.11"},
    "api_path": "/api/p_cap/instances/1/mode_command",
    "mode": 1,
    "min_delay_ns": 1000000,
    "jitter_on": 0,
    "packetloss_on": 0.0007,
    "state": {leg: {f: False for f in FUNCS} for leg in LEGS},
}

app = Flask(__name__)
lock = threading.Lock()
online = {leg: None for leg in LEGS}   # None = unbekannt
log = collections.deque(maxlen=20)


# ---------------------------------------------------------------- Konfiguration
def load_config():
    cfg = json.loads(json.dumps(DEFAULT_CONFIG))  # tiefe Kopie
    try:
        with open(CONFIG_FILE) as f:
            saved = json.load(f)
        for key in ("api_path", "mode", "min_delay_ns", "jitter_on", "packetloss_on"):
            if key in saved:
                cfg[key] = saved[key]
        for leg in LEGS:
            cfg["hosts"][leg] = saved.get("hosts", {}).get(leg, cfg["hosts"][leg])
            for f in FUNCS:
                cfg["state"][leg][f] = bool(saved.get("state", {}).get(leg, {}).get(f, False))
    except (OSError, ValueError):
        pass
    return cfg


def save_config(cfg):
    try:
        with open(CONFIG_FILE, "w") as f:
            json.dump(cfg, f, indent=2)
    except OSError as e:
        log_line(f"Konfiguration konnte nicht gespeichert werden: {e}")


def parse_number(text):
    """Int wenn moeglich, sonst Float. Erlaubt 1'000'000 / 1_000_000 / Leerzeichen."""
    text = str(text).strip().replace("'", "").replace("_", "").replace(" ", "")
    try:
        return int(text)
    except ValueError:
        return float(text)


def log_line(text):
    log.appendleft(time.strftime("%H:%M:%S  ") + text)


# ---------------------------------------------------------------- Geraete-API
def build_body(cfg, state):
    jitter = cfg["jitter_on"] if state["jitter"] else 0
    loss = cfg["packetloss_on"] if state["packetloss"] else 0
    return json.dumps([cfg["mode"], [cfg["min_delay_ns"], jitter, loss]],
                      separators=(",", ":"))


def send_command(cfg, leg, state):
    """PUT ans Geraet. Rueckgabe: (ok, meldung)."""
    host = cfg["hosts"][leg].strip()
    if not host:
        return False, "keine Adresse konfiguriert"
    url = host if host.startswith("http") else "http://" + host
    url = url.rstrip("/") + cfg["api_path"]
    body = build_body(cfg, state)
    req = urllib.request.Request(url, data=body.encode(), method="PUT",
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:
            ok = 200 <= resp.status < 300
            msg = f"HTTP {resp.status}"
    except urllib.error.HTTPError as e:
        ok, msg = False, f"HTTP {e.code}"
    except Exception as e:
        ok, msg = False, str(e)
    log_line(f"LEG {leg}  {body}  →  {msg}")
    return ok, msg


# ---------------------------------------------------------------- Ping-Thread
def ping_host(host):
    h = host.strip()
    for prefix in ("http://", "https://"):
        if h.startswith(prefix):
            h = h[len(prefix):]
    h = h.split("/")[0].split(":")[0]
    if not h:
        return False
    wait = "1000" if sys.platform == "darwin" else "1"  # macOS: ms, Linux/Busybox: s
    try:
        r = subprocess.run(["ping", "-c", "1", "-W", wait, h],
                           capture_output=True, timeout=3)
        return r.returncode == 0
    except Exception:
        return False


def pinger():
    while True:
        cfg = load_config()
        for leg in LEGS:
            online[leg] = ping_host(cfg["hosts"][leg])
        time.sleep(PING_INTERVAL)


# ---------------------------------------------------------------- API-Routen
@app.get("/api/status")
def api_status():
    cfg = load_config()
    return jsonify({"online": online, "hosts": cfg["hosts"],
                    "state": cfg["state"], "log": list(log)})


@app.post("/api/toggle")
def api_toggle():
    data = request.get_json(force=True)
    leg, func = data.get("leg"), data.get("func")
    if leg not in LEGS or func not in FUNCS:
        return jsonify({"ok": False, "msg": "ungültige Anfrage"}), 400
    with lock:
        cfg = load_config()
        new_state = dict(cfg["state"][leg])
        new_state[func] = not new_state[func]
        ok, msg = send_command(cfg, leg, new_state)
        if ok:
            cfg["state"][leg] = new_state
            save_config(cfg)
        return jsonify({"ok": ok, "msg": msg, "state": cfg["state"]})


@app.post("/api/resend")
def api_resend():
    """Aktuellen Sollzustand erneut ans Geraet senden (z.B. nach Geraete-Neustart)."""
    data = request.get_json(force=True)
    leg = data.get("leg")
    if leg not in LEGS:
        return jsonify({"ok": False, "msg": "ungültige Anfrage"}), 400
    with lock:
        cfg = load_config()
        ok, msg = send_command(cfg, leg, cfg["state"][leg])
        return jsonify({"ok": ok, "msg": msg, "state": cfg["state"]})


# ---------------------------------------------------------------- Seiten
INDEX_HTML = """<!doctype html>
<html lang="de"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Impairment Control</title>
<link rel="icon" type="image/svg+xml" href="/static/favicon.svg">
<link rel="icon" type="image/png" sizes="32x32" href="/static/favicon-32.png">
<link rel="apple-touch-icon" href="/static/apple-touch-icon.png">
<style>
  :root { color-scheme: dark; }
  * { box-sizing: border-box; }
  body { margin:0; font-family:-apple-system,'Segoe UI',Roboto,sans-serif;
         background:#14171c; color:#e8eaed; }
  header { display:flex; align-items:center; justify-content:space-between;
           padding:14px 22px; background:#1c2027; border-bottom:1px solid #2a2f38; }
  header h1 { font-size:18px; margin:0; letter-spacing:.5px; }
  header a { color:#8ab4f8; text-decoration:none; font-size:14px; }
  main { display:flex; gap:20px; flex-wrap:wrap; padding:22px; justify-content:center; }
  .card { background:#1c2027; border:1px solid #2a2f38; border-radius:12px;
          padding:20px 24px; width:300px; }
  .leghead { display:flex; align-items:center; justify-content:space-between; margin-bottom:4px; }
  .leghead h2 { margin:0; font-size:20px; }
  .dot { width:12px; height:12px; border-radius:50%; background:#777; display:inline-block; margin-right:6px; }
  .dot.on { background:#34c759; box-shadow:0 0 6px #34c759; }
  .dot.off { background:#ff453a; }
  .pingstate { font-size:13px; color:#9aa0a6; display:flex; align-items:center; }
  .host { font-size:12px; color:#9aa0a6; margin-bottom:16px; font-family:Menlo,monospace; }
  .row { display:flex; align-items:center; justify-content:space-between; padding:10px 0;
         border-top:1px solid #2a2f38; }
  .row span { font-size:15px; }
  .switch { position:relative; width:52px; height:28px; flex:none; }
  .switch input { opacity:0; width:0; height:0; }
  .slider { position:absolute; inset:0; background:#4a4f58; border-radius:14px;
            transition:.2s; cursor:pointer; }
  .slider:before { content:""; position:absolute; height:22px; width:22px; left:3px; top:3px;
                   background:#fff; border-radius:50%; transition:.2s; }
  input:checked + .slider { background:#e0a100; }
  input:checked + .slider:before { transform:translateX(24px); }
  input:disabled + .slider { opacity:.4; cursor:wait; }
  .resend { margin-top:14px; width:100%; background:#2a2f38; color:#c8ccd2; border:none;
            border-radius:8px; padding:8px; font-size:13px; cursor:pointer; }
  .resend:hover { background:#343a45; }
  #log { margin:0 22px 22px; background:#101317; border:1px solid #2a2f38; border-radius:10px;
         padding:12px 16px; font-family:Menlo,monospace; font-size:12px; color:#9aa0a6;
         min-height:60px; max-height:200px; overflow-y:auto; white-space:pre-wrap; }
  #toast { position:fixed; bottom:24px; left:50%; transform:translateX(-50%);
           background:#5c1f1f; color:#ffb4ab; padding:10px 18px; border-radius:8px;
           font-size:14px; opacity:0; transition:opacity .3s; pointer-events:none; }
</style></head>
<body>
<header>
  <h1>Impairment Control</h1>
  <a href="/setup">&#9881;&#65039; Setup</a>
</header>
<main>
  {% for leg in legs %}
  <div class="card">
    <div class="leghead">
      <h2>LEG {{ leg }}</h2>
      <span class="pingstate"><span class="dot" id="dot{{ leg }}"></span><span id="ping{{ leg }}">–</span></span>
    </div>
    <div class="host" id="host{{ leg }}"></div>
    <div class="row">
      <span>Packetloss</span>
      <label class="switch"><input type="checkbox" id="sw{{ leg }}packetloss"
        onchange="toggle('{{ leg }}','packetloss',this)"><span class="slider"></span></label>
    </div>
    <div class="row">
      <span>Jitter</span>
      <label class="switch"><input type="checkbox" id="sw{{ leg }}jitter"
        onchange="toggle('{{ leg }}','jitter',this)"><span class="slider"></span></label>
    </div>
    <button class="resend" onclick="resend('{{ leg }}')">Werte erneut senden</button>
  </div>
  {% endfor %}
</main>
<div id="log"></div>
<div id="toast"></div>
<script>
const LEGS = ["A","B"];
let busy = false;

function apply(s) {
  for (const leg of LEGS) {
    const dot = document.getElementById("dot"+leg);
    const ping = document.getElementById("ping"+leg);
    const on = s.online[leg];
    dot.className = "dot " + (on === true ? "on" : on === false ? "off" : "");
    ping.textContent = on === true ? "online" : on === false ? "offline" : "prüfe…";
    document.getElementById("host"+leg).textContent = s.hosts[leg];
    if (!busy) {
      document.getElementById("sw"+leg+"packetloss").checked = s.state[leg].packetloss;
      document.getElementById("sw"+leg+"jitter").checked = s.state[leg].jitter;
    }
  }
  document.getElementById("log").textContent = s.log.join("\\n") || "Noch keine Kommandos gesendet.";
}

async function refresh() {
  try { apply(await (await fetch("/api/status")).json()); } catch (e) {}
}

function toast(msg) {
  const t = document.getElementById("toast");
  t.textContent = msg; t.style.opacity = 1;
  setTimeout(() => t.style.opacity = 0, 4000);
}

async function toggle(leg, func, el) {
  busy = true; el.disabled = true;
  try {
    const r = await (await fetch("/api/toggle", {method:"POST",
      headers:{"Content-Type":"application/json"},
      body: JSON.stringify({leg, func})})).json();
    if (!r.ok) { el.checked = !el.checked; toast("LEG "+leg+" "+func+": "+r.msg); }
  } catch (e) { el.checked = !el.checked; toast("Server nicht erreichbar"); }
  el.disabled = false; busy = false;
  refresh();
}

async function resend(leg) {
  try {
    const r = await (await fetch("/api/resend", {method:"POST",
      headers:{"Content-Type":"application/json"},
      body: JSON.stringify({leg})})).json();
    if (!r.ok) toast("LEG "+leg+": "+r.msg);
  } catch (e) { toast("Server nicht erreichbar"); }
  refresh();
}

refresh();
setInterval(refresh, 2000);
</script>
</body></html>"""

SETUP_HTML = """<!doctype html>
<html lang="de"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Setup – Impairment Control</title>
<link rel="icon" type="image/svg+xml" href="/static/favicon.svg">
<link rel="icon" type="image/png" sizes="32x32" href="/static/favicon-32.png">
<link rel="apple-touch-icon" href="/static/apple-touch-icon.png">
<style>
  :root { color-scheme: dark; }
  * { box-sizing: border-box; }
  body { margin:0; font-family:-apple-system,'Segoe UI',Roboto,sans-serif;
         background:#14171c; color:#e8eaed; }
  header { display:flex; align-items:center; justify-content:space-between;
           padding:14px 22px; background:#1c2027; border-bottom:1px solid #2a2f38; }
  header h1 { font-size:18px; margin:0; }
  header a { color:#8ab4f8; text-decoration:none; font-size:14px; }
  form { max-width:480px; margin:26px auto; background:#1c2027; border:1px solid #2a2f38;
         border-radius:12px; padding:24px 28px; }
  label { display:block; font-size:13px; color:#9aa0a6; margin:14px 0 4px; }
  input { width:100%; padding:9px 12px; border-radius:8px; border:1px solid #3a404b;
          background:#101317; color:#e8eaed; font-size:15px; font-family:Menlo,monospace; }
  .hint { font-size:12px; color:#6e747c; margin-top:3px; }
  button { margin-top:22px; width:100%; padding:11px; border:none; border-radius:8px;
           background:#2e6bd6; color:#fff; font-size:15px; cursor:pointer; }
  button:hover { background:#3a78e6; }
  .err { color:#ffb4ab; margin-top:12px; font-size:14px; }
  h3 { margin:22px 0 0; font-size:14px; color:#c8ccd2; border-top:1px solid #2a2f38; padding-top:18px; }
</style></head>
<body>
<header>
  <h1>Setup</h1>
  <a href="/">&#8592; zurück zur Steuerung</a>
</header>
<form method="post">
  <h3 style="border:none;padding:0;margin-top:0">Geräte</h3>
  <label>LEG A – IP / Host</label>
  <input name="host_A" value="{{ cfg['hosts']['A'] }}">
  <label>LEG B – IP / Host</label>
  <input name="host_B" value="{{ cfg['hosts']['B'] }}">

  <h3>Impairment-Werte</h3>
  <label>Min. Delay (ns)</label>
  <input name="min_delay_ns" value="{{ cfg['min_delay_ns'] }}">
  <div class="hint">wird immer gesendet, z.B. 1'000'000</div>
  <label>Jitter (Wert bei ON)</label>
  <input name="jitter_on" value="{{ cfg['jitter_on'] }}">
  <div class="hint">bei OFF wird 0 gesendet</div>
  <label>Packetloss (Wert bei ON)</label>
  <input name="packetloss_on" value="{{ cfg['packetloss_on'] }}">
  <div class="hint">bei OFF wird 0 gesendet, z.B. 0.0007</div>

  <h3>Erweitert</h3>
  <label>API-Pfad</label>
  <input name="api_path" value="{{ cfg['api_path'] }}">
  <label>Mode</label>
  <input name="mode" value="{{ cfg['mode'] }}">

  {% if error %}<div class="err">{{ error }}</div>{% endif %}
  <button type="submit">Speichern &amp; an beide Legs senden</button>
</form>
</body></html>"""


@app.get("/")
def index():
    return render_template_string(INDEX_HTML, legs=LEGS)


@app.route("/setup", methods=["GET", "POST"])
def setup():
    with lock:
        cfg = load_config()
        error = None
        if request.method == "POST":
            try:
                cfg["hosts"]["A"] = request.form["host_A"].strip()
                cfg["hosts"]["B"] = request.form["host_B"].strip()
                cfg["api_path"] = request.form["api_path"].strip() or DEFAULT_CONFIG["api_path"]
                cfg["mode"] = parse_number(request.form["mode"])
                cfg["min_delay_ns"] = parse_number(request.form["min_delay_ns"])
                cfg["jitter_on"] = parse_number(request.form["jitter_on"])
                cfg["packetloss_on"] = parse_number(request.form["packetloss_on"])
            except (ValueError, KeyError):
                error = "Ungültiger Zahlenwert – bitte prüfen."
            if not error:
                save_config(cfg)
                log_line("Setup gespeichert – sende aktuelle Werte an beide Legs")
                for leg in LEGS:
                    send_command(cfg, leg, cfg["state"][leg])
                return redirect("/")
        return render_template_string(SETUP_HTML, cfg=cfg, error=error)


if __name__ == "__main__":
    if not os.path.exists(CONFIG_FILE):
        save_config(load_config())
    threading.Thread(target=pinger, daemon=True).start()
    app.run(host="0.0.0.0", port=8080, threaded=True)
