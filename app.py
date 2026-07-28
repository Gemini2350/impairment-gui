#!/usr/bin/env python3
"""Web GUI: toggle network impairment (packet loss / jitter) on LEG A and LEG B.

Sends  PUT <host>/api/p_cap/instances/1/mode_command  with body
[mode, [min_delay_ns, jitter, packetloss]]   e.g.  [1,[1000000,0,0.0007]]
"""

import base64
import collections
import json
import os
import re
import ssl
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request

from flask import Flask, jsonify, redirect, render_template_string, request

DATA_DIR = os.environ.get("DATA_DIR") or os.path.dirname(os.path.abspath(__file__))
CONFIG_FILE = os.path.join(DATA_DIR, "config.json")
PING_INTERVAL = 3          # seconds between ping rounds
HTTP_TIMEOUT = 5           # timeout for the PUT to the device
LEGS = ("A", "B")
FUNCS = ("packetloss", "jitter")
PORT_KEYS = ("gmB2", "gmB3", "legA", "legB")   # switch ports controlled via eAPI

DEFAULT_CONFIG = {
    "hosts": {"A": "192.168.1.10", "B": "192.168.1.11"},
    "api_path": "/api/p_cap/instances/{instance}/mode_command",
    "mode": 1,
    "instances": {"packetloss": "1", "jitter": "1"},   # each: "0" | "1" | "both"
    "min_delay_ns": 1000000,
    "jitter_on": 0,
    "packetloss_on": 0.0007,
    "state": {leg: {f: False for f in FUNCS} for leg in LEGS},
    "ptp": {"switch": "", "username": "", "password": "",
            "interfaces": {"gmB2": "Ethernet47", "gmB3": "Ethernet48",
                           "legA": "Ethernet49/1", "legB": "Ethernet50/1"}},
    "hue": {"bridge": "", "key": "", "light": ""},
}

app = Flask(__name__)
lock = threading.Lock()
online = {leg: None for leg in LEGS}   # None = unknown
ptp_status = {"configured": False, "reachable": None, "ports": {}}
hue_status = {"configured": False, "reachable": None, "on": None, "name": "", "msg": ""}
log = collections.deque(maxlen=20)


# ---------------------------------------------------------------- configuration
def load_config():
    cfg = json.loads(json.dumps(DEFAULT_CONFIG))  # deep copy
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
        saved_ptp = saved.get("ptp", {})
        for key in ("switch", "username", "password"):
            cfg["ptp"][key] = saved_ptp.get(key, cfg["ptp"][key])
        saved_if = saved_ptp.get("interfaces", {})
        if not saved_if and saved_ptp.get("interface"):
            saved_if = {"gmB2": saved_ptp["interface"]}   # legacy single-port config
        for k in PORT_KEYS:
            cfg["ptp"]["interfaces"][k] = saved_if.get(k, cfg["ptp"]["interfaces"][k])
        for key in ("bridge", "key", "light"):
            cfg["hue"][key] = saved.get("hue", {}).get(key, cfg["hue"][key])
        for f in FUNCS:
            val = saved.get("instances", {}).get(f, cfg["instances"][f])
            cfg["instances"][f] = val if val in ("0", "1", "both") else "1"
    except (OSError, ValueError):
        pass
    return cfg


def save_config(cfg):
    try:
        with open(CONFIG_FILE, "w") as f:
            json.dump(cfg, f, indent=2)
    except OSError as e:
        log_line(f"Could not save configuration: {e}")


def parse_number(text):
    """Int if possible, float otherwise. Accepts 1'000'000 / 1_000_000 / spaces."""
    text = str(text).strip().replace("'", "").replace("_", "").replace(" ", "")
    try:
        return int(text)
    except ValueError:
        return float(text)


def log_line(text):
    log.appendleft(time.strftime("%H:%M:%S  ") + text)


# ---------------------------------------------------------------- device API
def target_instances(cfg):
    """All p_cap instances addressed by the current instance settings."""
    insts = set()
    for f in FUNCS:
        sel = cfg["instances"][f]
        insts |= {"0", "1"} if sel == "both" else {sel}
    return sorted(insts)


def build_body(cfg, state, inst):
    """Command body for one instance: a value is only set if its function is ON
    and configured to target this instance, otherwise 0."""
    def active(f):
        sel = cfg["instances"][f]
        return state[f] and (sel == "both" or sel == inst)
    jitter = cfg["jitter_on"] if active("jitter") else 0
    loss = cfg["packetloss_on"] if active("packetloss") else 0
    return json.dumps([cfg["mode"], [cfg["min_delay_ns"], jitter, loss]],
                      separators=(",", ":"))


def render_path(template, inst):
    if "{instance}" in template:
        return template.replace("{instance}", inst)
    # legacy paths with a fixed instance number, e.g. /instances/1/mode_command
    return re.sub(r"/instances/\d+/", f"/instances/{inst}/", template, count=1)


def send_command(cfg, leg, state):
    """PUT to every targeted instance of the device. Returns (ok, message)."""
    host = cfg["hosts"][leg].strip()
    if not host:
        return False, "no address configured"
    base = host if host.startswith("http") else "http://" + host
    ok_all, msgs = True, []
    for inst in target_instances(cfg):
        url = base.rstrip("/") + render_path(cfg["api_path"], inst)
        body = build_body(cfg, state, inst)
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
        log_line(f"LEG {leg}  inst {inst}  {body}  →  {msg}")
        ok_all = ok_all and ok
        msgs.append(f"inst {inst}: {msg}")
    return ok_all, "; ".join(msgs)


# ---------------------------------------------------------------- Arista eAPI
def eapi_call(cfg, cmds):
    """JSON-RPC runCmds against the Arista switch. Returns (ok, result_or_message)."""
    p = cfg["ptp"]
    host = p["switch"].strip()
    if not host:
        return False, "no switch configured"
    url = host if host.startswith("http") else "https://" + host
    url = url.rstrip("/") + "/command-api"
    payload = {"jsonrpc": "2.0", "method": "runCmds", "id": "1",
               "params": {"version": 1, "cmds": cmds, "format": "json"}}
    auth = base64.b64encode(f"{p['username']}:{p['password']}".encode()).decode()
    req = urllib.request.Request(url, data=json.dumps(payload).encode(),
                                 headers={"Content-Type": "application/json",
                                          "Authorization": "Basic " + auth})
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE   # lab switches use self-signed certs
    try:
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT, context=ctx) as resp:
            data = json.load(resp)
    except urllib.error.HTTPError as e:
        return False, "authentication failed" if e.code == 401 else f"HTTP {e.code}"
    except Exception as e:
        return False, str(e)
    if "error" in data:
        return False, data["error"].get("message", "eAPI error")
    return True, data.get("result", [])


def ptp_refresh(cfg):
    """Read the real admin/link state of all configured switch ports (one call)."""
    ifaces = {k: v.strip() for k, v in cfg["ptp"]["interfaces"].items() if v.strip()}
    if not cfg["ptp"]["switch"].strip() or not ifaces:
        ptp_status.update({"configured": False, "reachable": None, "ports": {}})
        return
    ok, res = eapi_call(cfg, [f"show interfaces {','.join(ifaces.values())} status"])
    if not ok:
        ptp_status.update({"configured": True, "reachable": False,
                           "ports": {k: {"interface": v, "up": None, "link": str(res)}
                                     for k, v in ifaces.items()}})
        return
    try:
        statuses = res[0]["interfaceStatuses"]
    except (KeyError, IndexError, TypeError):
        statuses = {}
    ports = {}
    for k, v in ifaces.items():
        st = statuses.get(v)
        if st is None:
            ports[k] = {"interface": v, "up": None, "link": "not found"}
        else:
            link = st.get("linkStatus", "unknown")
            ports[k] = {"interface": v, "up": link != "disabled", "link": link}
    ptp_status.update({"configured": True, "reachable": True, "ports": ports})


def ptp_poller():
    while True:
        try:
            ptp_refresh(load_config())
        except Exception:
            pass
        time.sleep(PING_INTERVAL)


# ---------------------------------------------------------------- Philips Hue
def hue_request(bridge, path, method="GET", payload=None):
    """HTTP call against the Hue bridge (v1 API). Returns (ok, parsed_json_or_msg)."""
    bridge = bridge.strip()
    if not bridge:
        return False, "no bridge configured"
    base = bridge if bridge.startswith("http") else "http://" + bridge
    url = base.rstrip("/") + "/api" + path
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE   # newer bridges use a self-signed cert on https
    try:
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT, context=ctx) as resp:
            return True, json.load(resp)
    except Exception as e:
        return False, str(e)


def hue_refresh(cfg):
    """Read the real on/off state of the plug from the bridge."""
    h = cfg["hue"]
    if not (h["bridge"].strip() and h["key"].strip() and str(h["light"]).strip()):
        hue_status.update({"configured": False, "reachable": None, "on": None,
                           "name": "", "msg": ""})
        return
    ok, res = hue_request(h["bridge"], f"/{h['key']}/lights/{h['light']}")
    if ok and isinstance(res, dict) and "state" in res:
        hue_status.update({"configured": True, "reachable": True,
                           "on": bool(res["state"].get("on")),
                           "name": res.get("name", ""), "msg": ""})
        return
    if ok and isinstance(res, list) and res and "error" in res[0]:
        msg = res[0]["error"].get("description", "bridge error")
    else:
        msg = str(res) if not ok else "unexpected reply"
    hue_status.update({"configured": True, "reachable": False, "on": None,
                       "name": "", "msg": msg})


def hue_poller():
    while True:
        try:
            hue_refresh(load_config())
        except Exception:
            pass
        time.sleep(PING_INTERVAL)


# ---------------------------------------------------------------- ping thread
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


# ---------------------------------------------------------------- API routes
@app.get("/api/status")
def api_status():
    cfg = load_config()
    ptp = dict(ptp_status)
    ptp["switch"] = cfg["ptp"]["switch"]
    hue = dict(hue_status)
    hue["bridge"] = cfg["hue"]["bridge"]
    hue["light"] = cfg["hue"]["light"]
    return jsonify({"online": online, "hosts": cfg["hosts"],
                    "state": cfg["state"], "ptp": ptp, "hue": hue,
                    "log": list(log)})


@app.post("/api/toggle")
def api_toggle():
    data = request.get_json(force=True)
    leg, func = data.get("leg"), data.get("func")
    if leg not in LEGS or func not in FUNCS:
        return jsonify({"ok": False, "msg": "invalid request"}), 400
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
    """Re-send the current target state to the device (e.g. after a device reboot)."""
    data = request.get_json(force=True)
    leg = data.get("leg")
    if leg not in LEGS:
        return jsonify({"ok": False, "msg": "invalid request"}), 400
    with lock:
        cfg = load_config()
        ok, msg = send_command(cfg, leg, cfg["state"][leg])
        return jsonify({"ok": ok, "msg": msg, "state": cfg["state"]})


@app.post("/api/ptp")
def api_ptp():
    """Enable/disable one switch port (no shutdown / shutdown)."""
    data = request.get_json(force=True)
    port = data.get("port")
    on = bool(data.get("on"))
    if port not in PORT_KEYS:
        return jsonify({"ok": False, "msg": "invalid request"}), 400
    with lock:
        cfg = load_config()
        intf = cfg["ptp"]["interfaces"].get(port, "").strip()
        if not intf:
            return jsonify({"ok": False, "msg": "no interface configured"})
        cmd = "no shutdown" if on else "shutdown"
        ok, res = eapi_call(cfg, ["enable", "configure",
                                  f"interface {intf}", cmd])
        msg = "OK" if ok else str(res)
        log_line(f"SWITCH  {intf} {cmd}  →  {msg}")
        if ok:
            ptp_refresh(cfg)
        return jsonify({"ok": ok, "msg": msg, "ptp": ptp_status})


@app.post("/api/hue")
def api_hue():
    """Switch the Hue plug on/off."""
    on = bool(request.get_json(force=True).get("on"))
    with lock:
        cfg = load_config()
        h = cfg["hue"]
        ok, res = hue_request(h["bridge"], f"/{h['key']}/lights/{h['light']}/state",
                              "PUT", {"on": on})
        if ok and isinstance(res, list) and any("error" in r for r in res):
            ok = False
            res = "; ".join(r["error"].get("description", "error")
                            for r in res if "error" in r)
        msg = "OK" if ok else str(res)
        log_line(f"HUE  plug {h['light']} {'on' if on else 'off'}  →  {msg}")
        if ok:
            hue_refresh(cfg)
        return jsonify({"ok": ok, "msg": msg, "hue": hue_status})


@app.post("/api/hue/pair")
def api_hue_pair():
    """Create an application key (user must press the bridge link button first)."""
    bridge = (request.get_json(force=True).get("bridge") or "").strip()
    with lock:
        cfg = load_config()
        if bridge:
            cfg["hue"]["bridge"] = bridge
        ok, res = hue_request(cfg["hue"]["bridge"], "", "POST",
                              {"devicetype": "impairment-gui#docker"})
        if ok and isinstance(res, list) and res and "success" in res[0]:
            cfg["hue"]["key"] = res[0]["success"]["username"]
            save_config(cfg)
            log_line("HUE  paired with bridge, application key saved")
            return jsonify({"ok": True, "key": cfg["hue"]["key"]})
        if ok and isinstance(res, list) and res and "error" in res[0]:
            msg = res[0]["error"].get("description", "pairing failed")
        else:
            msg = str(res)
        return jsonify({"ok": False, "msg": msg})


@app.post("/api/hue/lights")
def api_hue_lights():
    """List plugs/lights known to the bridge (id → name), for the setup page."""
    data = request.get_json(force=True)
    cfg = load_config()
    bridge = (data.get("bridge") or "").strip() or cfg["hue"]["bridge"]
    key = (data.get("key") or "").strip() or cfg["hue"]["key"]
    ok, res = hue_request(bridge, f"/{key}/lights")
    if ok and isinstance(res, dict):
        return jsonify({"ok": True, "lights": {i: v.get("name", "?")
                                               for i, v in sorted(res.items())}})
    if ok and isinstance(res, list) and res and "error" in res[0]:
        msg = res[0]["error"].get("description", "failed")
    else:
        msg = str(res)
    return jsonify({"ok": False, "msg": msg, "lights": {}})


# ---------------------------------------------------------------- pages
INDEX_HTML = """<!doctype html>
<html lang="en"><head>
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
  #ptpcard input:checked + .slider, #powercard input:checked + .slider,
  .portsw:checked + .slider { background:#2e9e4f; }
  .dot.warn { background:#e0a100; box-shadow:0 0 6px #e0a100; }
  .rowright { display:flex; align-items:center; gap:8px; }
  .rowstate { font-size:11px; color:#9aa0a6; }
  .note { font-size:12px; color:#9aa0a6; margin-top:12px; line-height:1.45;
          border-top:1px solid #2a2f38; padding-top:10px; }
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
      <span>Packet loss</span>
      <label class="switch"><input type="checkbox" id="sw{{ leg }}packetloss"
        onchange="toggle('{{ leg }}','packetloss',this)"><span class="slider"></span></label>
    </div>
    <div class="row">
      <span>Jitter</span>
      <label class="switch"><input type="checkbox" id="sw{{ leg }}jitter"
        onchange="toggle('{{ leg }}','jitter',this)"><span class="slider"></span></label>
    </div>
    <div class="row" id="rowport{{ leg }}" style="display:none">
      <span id="portname{{ leg }}">Switch port</span>
      <span class="rowright"><span class="rowstate" id="portstate{{ leg }}"></span>
      <label class="switch"><input type="checkbox" class="portsw" id="swport{{ leg }}"
        onchange="portToggle('leg{{ leg }}', this)"><span class="slider"></span></label></span>
    </div>
    <button class="resend" onclick="resend('{{ leg }}')">Resend values</button>
  </div>
  {% endfor %}
  <div class="card" id="ptpcard">
    <div class="leghead">
      <h2>Main PTP</h2>
      <span class="pingstate"><span class="dot" id="dotPTP"></span><span id="pingPTP">–</span></span>
    </div>
    <div class="host" id="hostPTP"></div>
    <div class="row" id="rowgmB2" style="display:none">
      <span>PTP GM B2</span>
      <span class="rowright"><span class="rowstate" id="stgmB2"></span>
      <label class="switch"><input type="checkbox" id="swgmB2"
        onchange="portToggle('gmB2', this)"><span class="slider"></span></label></span>
    </div>
    <div class="row" id="rowgmB3" style="display:none">
      <span>PTP GM B3</span>
      <span class="rowright"><span class="rowstate" id="stgmB3"></span>
      <label class="switch"><input type="checkbox" id="swgmB3"
        onchange="portToggle('gmB3', this)"><span class="slider"></span></label></span>
    </div>
  </div>
  <div class="card" id="powercard">
    <div class="leghead">
      <h2>Power</h2>
      <span class="pingstate"><span class="dot" id="dotHUE"></span><span id="pingHUE">–</span></span>
    </div>
    <div class="host" id="hostHUE"></div>
    <div class="row">
      <span>Power on</span>
      <label class="switch"><input type="checkbox" id="swHUE"
        onchange="hueToggle(this)"><span class="slider"></span></label>
    </div>
    <div class="note">&#9432; Allow ~10 min warm-up after switching on.<br>
      Please switch off again when finished (heat &amp; power consumption).</div>
  </div>
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
    ping.textContent = on === true ? "online" : on === false ? "offline" : "checking…";
    document.getElementById("host"+leg).textContent = s.hosts[leg];
    if (!busy) {
      document.getElementById("sw"+leg+"packetloss").checked = s.state[leg].packetloss;
      document.getElementById("sw"+leg+"jitter").checked = s.state[leg].jitter;
    }
  }
  applyPtp(s.ptp);
  applyHue(s.hue);
  document.getElementById("log").textContent = s.log.join("\\n") || "No commands sent yet.";
}

function applyHue(h) {
  const dot = document.getElementById("dotHUE");
  const txt = document.getElementById("pingHUE");
  const sw = document.getElementById("swHUE");
  document.getElementById("hostHUE").textContent = h.configured
    ? (h.name ? h.name + " – " : "") + "plug " + h.light + " @ " + h.bridge
    : "not configured – see Setup";
  let cls = "", label = "–";
  if (!h.configured)              { label = "–"; }
  else if (h.reachable === false) { cls = "off";  label = h.msg || "bridge unreachable"; }
  else if (h.on === true)         { cls = "on";   label = "on"; }
  else if (h.on === false)        { cls = "warn"; label = "off"; }
  else                            { label = "checking…"; }
  dot.className = "dot " + cls;
  txt.textContent = label;
  if (!busy) sw.checked = h.on === true;
  sw.disabled = busy || !h.configured || h.reachable === false;
}

async function hueToggle(el) {
  const want = el.checked;
  if (!want && !confirm("Cut power via the Hue plug?")) {
    el.checked = true;
    return;
  }
  busy = true; el.disabled = true;
  try {
    const r = await (await fetch("/api/hue", {method:"POST",
      headers:{"Content-Type":"application/json"},
      body: JSON.stringify({on: want})})).json();
    if (!r.ok) { el.checked = !want; toast("Power: " + r.msg); }
  } catch (e) { el.checked = !want; toast("Server unreachable"); }
  busy = false; el.disabled = false;
  refresh();
}

let lastPtp = null;
const PORTMAP = {
  gmB2: ["rowgmB2", "swgmB2", "stgmB2", null],
  gmB3: ["rowgmB3", "swgmB3", "stgmB3", null],
  legA: ["rowportA", "swportA", "portstateA", "portnameA"],
  legB: ["rowportB", "swportB", "portstateB", "portnameB"],
};

function applyPtp(p) {
  lastPtp = p;
  const dot = document.getElementById("dotPTP");
  const txt = document.getElementById("pingPTP");
  document.getElementById("hostPTP").textContent =
    p.configured ? p.switch : "not configured – see Setup";
  let cls = "", label = "–";
  if (!p.configured)              { label = "–"; }
  else if (p.reachable === false) { cls = "off"; label = "switch unreachable"; }
  else if (p.reachable === true)  { cls = "on";  label = "connected"; }
  else                            { label = "checking…"; }
  dot.className = "dot " + cls;
  txt.textContent = label;
  for (const [key, [rowId, swId, stId, nameId]] of Object.entries(PORTMAP)) {
    const row = document.getElementById(rowId);
    const port = p.ports ? p.ports[key] : null;
    if (!port) { row.style.display = "none"; continue; }
    row.style.display = "";
    if (nameId) document.getElementById(nameId).textContent = port.interface;
    document.getElementById(stId).textContent =
      port.up === true ? (port.link === "connected" ? "up" : port.link)
      : port.up === false ? "admin down" : "–";
    const sw = document.getElementById(swId);
    if (!busy) sw.checked = port.up === true;
    sw.disabled = busy || p.reachable !== true;
  }
}

async function portToggle(key, el) {
  const want = el.checked;
  const iface = lastPtp && lastPtp.ports && lastPtp.ports[key]
    ? lastPtp.ports[key].interface : "this port";
  if (!want && !confirm("Shut down " + iface + " on the switch?")) {
    el.checked = true;
    return;
  }
  busy = true; el.disabled = true;
  try {
    const r = await (await fetch("/api/ptp", {method:"POST",
      headers:{"Content-Type":"application/json"},
      body: JSON.stringify({port: key, on: want})})).json();
    if (!r.ok) { el.checked = !want; toast(iface + ": " + r.msg); }
  } catch (e) { el.checked = !want; toast("Server unreachable"); }
  busy = false; el.disabled = false;
  refresh();
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
  } catch (e) { el.checked = !el.checked; toast("Server unreachable"); }
  el.disabled = false; busy = false;
  refresh();
}

async function resend(leg) {
  try {
    const r = await (await fetch("/api/resend", {method:"POST",
      headers:{"Content-Type":"application/json"},
      body: JSON.stringify({leg})})).json();
    if (!r.ok) toast("LEG "+leg+": "+r.msg);
  } catch (e) { toast("Server unreachable"); }
  refresh();
}

refresh();
setInterval(refresh, 2000);
</script>
</body></html>"""

SETUP_HTML = """<!doctype html>
<html lang="en"><head>
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
  input, select { width:100%; padding:9px 12px; border-radius:8px; border:1px solid #3a404b;
          background:#101317; color:#e8eaed; font-size:15px; font-family:Menlo,monospace; }
  .hint { font-size:12px; color:#6e747c; margin-top:3px; }
  button { margin-top:22px; width:100%; padding:11px; border:none; border-radius:8px;
           background:#2e6bd6; color:#fff; font-size:15px; cursor:pointer; }
  button:hover { background:#3a78e6; }
  .mini { width:auto; margin-top:12px; background:#2a2f38; color:#c8ccd2;
          font-size:13px; padding:8px 12px; }
  .mini:hover { background:#343a45; }
  .err { color:#ffb4ab; margin-top:12px; font-size:14px; }
  h3 { margin:22px 0 0; font-size:14px; color:#c8ccd2; border-top:1px solid #2a2f38; padding-top:18px; }
</style></head>
<body>
<header>
  <h1>Setup</h1>
  <a href="/">&#8592; back to control</a>
</header>
<form method="post">
  <h3 style="border:none;padding:0;margin-top:0">Devices</h3>
  <label>LEG A – IP / host</label>
  <input name="host_A" value="{{ cfg['hosts']['A'] }}">
  <label>LEG B – IP / host</label>
  <input name="host_B" value="{{ cfg['hosts']['B'] }}">

  <h3>Impairment values</h3>
  <label>Min. delay (ns)</label>
  <input name="min_delay_ns" value="{{ cfg['min_delay_ns'] }}">
  <div class="hint">always sent, e.g. 1'000'000</div>
  <label>Jitter (value when ON)</label>
  <input name="jitter_on" value="{{ cfg['jitter_on'] }}">
  <div class="hint">0 is sent when OFF</div>
  <label>Jitter – instance</label>
  <select name="inst_jitter">
    {% for v in ['0', '1', 'both'] %}<option value="{{ v }}"
      {% if cfg['instances']['jitter'] == v %}selected{% endif %}>{{ v }}</option>{% endfor %}
  </select>
  <label>Packet loss (value when ON)</label>
  <input name="packetloss_on" value="{{ cfg['packetloss_on'] }}">
  <div class="hint">0 is sent when OFF, e.g. 0.0007</div>
  <label>Packet loss – instance</label>
  <select name="inst_packetloss">
    {% for v in ['0', '1', 'both'] %}<option value="{{ v }}"
      {% if cfg['instances']['packetloss'] == v %}selected{% endif %}>{{ v }}</option>{% endfor %}
  </select>

  <h3>Main PTP – Arista switch (eAPI)</h3>
  <label>Switch – IP / host</label>
  <input name="ptp_switch" value="{{ cfg['ptp']['switch'] }}">
  <div class="hint">HTTPS with self-signed certificate accepted; leave empty to disable the card</div>
  <label>Interface – PTP GM B2</label>
  <input name="ptp_if_gmB2" value="{{ cfg['ptp']['interfaces']['gmB2'] }}">
  <label>Interface – PTP GM B3</label>
  <input name="ptp_if_gmB3" value="{{ cfg['ptp']['interfaces']['gmB3'] }}">
  <label>Interface – LEG A port</label>
  <input name="ptp_if_legA" value="{{ cfg['ptp']['interfaces']['legA'] }}">
  <label>Interface – LEG B port</label>
  <input name="ptp_if_legB" value="{{ cfg['ptp']['interfaces']['legB'] }}">
  <div class="hint">leave an interface empty to hide its switch in the UI</div>
  <label>eAPI username</label>
  <input name="ptp_username" value="{{ cfg['ptp']['username'] }}">
  <label>eAPI password</label>
  <input name="ptp_password" type="password" placeholder="(unchanged)">

  <h3>Power switch – Philips Hue</h3>
  <label>Bridge – IP / host</label>
  <input name="hue_bridge" value="{{ cfg['hue']['bridge'] }}">
  <div class="hint">leave empty to disable the card</div>
  <label>Application key</label>
  <input name="hue_key" value="{{ cfg['hue']['key'] }}">
  <div class="hint">no key yet? press the link button on the bridge, then click Pair below</div>
  <label>Plug / light ID</label>
  <input name="hue_light" value="{{ cfg['hue']['light'] }}">
  <div style="display:flex;gap:10px">
    <button type="button" class="mini" onclick="huePair()">Pair with bridge</button>
    <button type="button" class="mini" onclick="hueLights()">List plugs &amp; lights</button>
  </div>
  <div class="hint" id="hueinfo"></div>

  <h3>Advanced</h3>
  <label>API path</label>
  <input name="api_path" value="{{ cfg['api_path'] }}">
  <div class="hint">{instance} is replaced with the instance number</div>
  <label>Mode</label>
  <input name="mode" value="{{ cfg['mode'] }}">

  {% if error %}<div class="err">{{ error }}</div>{% endif %}
  <button type="submit">Save &amp; send to both legs</button>
</form>
<script>
async function huePair() {
  const bridge = document.querySelector('[name=hue_bridge]').value;
  const el = document.getElementById('hueinfo');
  el.textContent = 'Pairing…';
  try {
    const r = await (await fetch('/api/hue/pair', {method:'POST',
      headers:{'Content-Type':'application/json'},
      body: JSON.stringify({bridge})})).json();
    if (r.ok) {
      document.querySelector('[name=hue_key]').value = r.key;
      el.textContent = 'Paired – key filled in. Now pick the plug ID and save.';
    } else {
      el.textContent = 'Pairing failed: ' + r.msg;
    }
  } catch (e) { el.textContent = 'Server unreachable'; }
}
async function hueLights() {
  const bridge = document.querySelector('[name=hue_bridge]').value;
  const key = document.querySelector('[name=hue_key]').value;
  const el = document.getElementById('hueinfo');
  el.textContent = 'Loading…';
  try {
    const r = await (await fetch('/api/hue/lights', {method:'POST',
      headers:{'Content-Type':'application/json'},
      body: JSON.stringify({bridge, key})})).json();
    el.textContent = r.ok
      ? (Object.entries(r.lights).map(([i, n]) => i + ': ' + n).join('   |   ') || 'nothing found')
      : 'Failed: ' + r.msg;
  } catch (e) { el.textContent = 'Server unreachable'; }
}
</script>
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
                for f in FUNCS:
                    val = request.form.get("inst_" + f, "1")
                    cfg["instances"][f] = val if val in ("0", "1", "both") else "1"
                cfg["ptp"]["switch"] = request.form["ptp_switch"].strip()
                for k in PORT_KEYS:
                    cfg["ptp"]["interfaces"][k] = request.form.get("ptp_if_" + k, "").strip()
                cfg["ptp"]["username"] = request.form["ptp_username"].strip()
                if request.form.get("ptp_password"):
                    cfg["ptp"]["password"] = request.form["ptp_password"]
                cfg["hue"]["bridge"] = request.form["hue_bridge"].strip()
                cfg["hue"]["key"] = request.form["hue_key"].strip()
                cfg["hue"]["light"] = request.form["hue_light"].strip()
            except (ValueError, KeyError):
                error = "Invalid number – please check your input."
            if not error:
                save_config(cfg)
                log_line("Setup saved – sending current values to both legs")
                for leg in LEGS:
                    send_command(cfg, leg, cfg["state"][leg])
                return redirect("/")
        return render_template_string(SETUP_HTML, cfg=cfg, error=error)


if __name__ == "__main__":
    if not os.path.exists(CONFIG_FILE):
        save_config(load_config())
    threading.Thread(target=pinger, daemon=True).start()
    threading.Thread(target=ptp_poller, daemon=True).start()
    threading.Thread(target=hue_poller, daemon=True).start()
    app.run(host="0.0.0.0", port=8080, threaded=True)
