#!/usr/bin/env python3
"""Locate cell towers from timing advance (TA) while driving — and watch it happen.

Timing advance is the tower's own range measurement: it tells the modem how early to transmit so the
signal arrives in the tower's slot. One LTE TA step is 16 Ts ≈ 0.52 µs round trip ≈ 78.12 m of one-way
distance. Every observation therefore says "the tower is between TA·78 m and (TA+1)·78 m from here".
Collect a few dozen of those along a drive and the annuli intersect at the mast.

Inputs (pick one):
  --sim                 synthetic drive past three towers (TA quantised + NLOS bias), for demos/tests
  --replay FILE.csv     replay a recorded drive (ts,lat,lon,cell,band,pci,ta,rsrp), at --speed x real time
  --diag [HOST]         read TA live from the modem via its diag-router streamed over TCP (default root@192.168.8.1)

Output: live visualisation at http://localhost:8766, and every observation appended to ~/ta-observations.csv.

Engine: per cell, a robust least-squares fit of the tower position to the range annuli (Gauss–Newton in a
local metre frame, Huber loss, TA treated as a lower bound with a small NLOS prior), initialised from the
power-weighted RSRP centroid when available, otherwise from the closest observation. The uncertainty
ellipse comes from the Jacobian at the solution scaled by the residual RMS.
"""
import argparse, csv, json, math, os, random, subprocess, sys, threading, time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

TA_M = 78.12          # metres per LTE TA step (15 kHz SCS); NR 30 kHz SCS would be 39.06
OBS_CSV = os.path.expanduser("~/ta-observations.csv")
FIELDS = ["ts", "lat", "lon", "cell", "band", "pci", "ta", "rsrp"]

state = {"obs": [], "towers": {}, "truth": [], "source": "", "status": "starting", "pos": None, "browser_pos": None}
lock = threading.Lock()


def sh(cmd, timeout=12):
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout).stdout
    except Exception:
        return ""


# ----------------------------------------------------------------------------- geometry
def enu(lat, lon, lat0, lon0):
    """metres east/north of (lat0, lon0)"""
    return ((lon - lon0) * 111320.0 * math.cos(math.radians(lat0)), (lat - lat0) * 111320.0)


def lla(e, n, lat0, lon0):
    return (lat0 + n / 111320.0, lon0 + e / (111320.0 * math.cos(math.radians(lat0))))


def fit_tower(obs):
    """obs: list of dicts with lat, lon, ta, rsrp (rsrp may be None). Returns dict or None."""
    if len(obs) < 3:
        return None
    lat0, lon0 = obs[0]["lat"], obs[0]["lon"]
    pts = [enu(o["lat"], o["lon"], lat0, lon0) for o in obs]
    # range annulus centre: TA·78 + 39 m, plus ~30 m NLOS/processing bias typical for macro cells
    rng = [o["ta"] * TA_M + TA_M / 2 + 30.0 for o in obs]
    # init: RSRP-weighted centroid, else mean of the smallest-TA points
    ws = [10 ** (o["rsrp"] / 10) if o.get("rsrp") is not None else None for o in obs]
    if all(w is not None for w in ws):
        W = sum(ws); x = sum(w * p[0] for w, p in zip(ws, pts)) / W; y = sum(w * p[1] for w, p in zip(ws, pts)) / W
    else:
        k = sorted(range(len(obs)), key=lambda i: rng[i])[:max(3, len(obs) // 4)]
        x = sum(pts[i][0] for i in k) / len(k); y = sum(pts[i][1] for i in k) / len(k)
    # nudge the init off the points so the gradient is defined
    x += 25.0; y += 25.0
    huber = 60.0
    for _ in range(60):
        JTJ = [[0.0, 0.0], [0.0, 0.0]]; JTr = [0.0, 0.0]
        for (px, py), r in zip(pts, rng):
            dx, dy = x - px, y - py; d = math.hypot(dx, dy) or 1e-6
            res = d - r
            w = 1.0 if abs(res) <= huber else huber / abs(res)
            jx, jy = dx / d, dy / d
            JTJ[0][0] += w * jx * jx; JTJ[0][1] += w * jx * jy; JTJ[1][1] += w * jy * jy
            JTr[0] += w * jx * res; JTr[1] += w * jy * res
        JTJ[1][0] = JTJ[0][1]
        det = JTJ[0][0] * JTJ[1][1] - JTJ[0][1] * JTJ[1][0]
        if abs(det) < 1e-9:
            break
        sx = (JTJ[1][1] * JTr[0] - JTJ[0][1] * JTr[1]) / det
        sy = (-JTJ[1][0] * JTr[0] + JTJ[0][0] * JTr[1]) / det
        x -= sx; y -= sy
        if math.hypot(sx, sy) < 0.05:
            break
    res = [math.hypot(x - px, y - py) - r for (px, py), r in zip(pts, rng)]
    rms = math.sqrt(sum(v * v for v in res) / len(res))
    # covariance ≈ σ² (JᵀJ)⁻¹ ; ellipse from eigen-decomposition
    sigma2 = max(rms, TA_M / 3) ** 2
    if abs(det) < 1e-9:
        cov = [[1e6, 0], [0, 1e6]]
    else:
        cov = [[sigma2 * JTJ[1][1] / det, -sigma2 * JTJ[0][1] / det], [-sigma2 * JTJ[1][0] / det, sigma2 * JTJ[0][0] / det]]
    tr = cov[0][0] + cov[1][1]; dt = cov[0][0] * cov[1][1] - cov[0][1] * cov[1][0]
    disc = max(0.0, tr * tr / 4 - dt) ** 0.5
    l1, l2 = tr / 2 + disc, max(1e-6, tr / 2 - disc)
    ang = 0.5 * math.atan2(2 * cov[0][1], cov[0][0] - cov[1][1])
    lat, lon = lla(x, y, lat0, lon0)
    return {"lat": lat, "lon": lon, "n": len(obs), "rms_m": round(rms, 1), "ell_a": round(2 * math.sqrt(l1), 1), "ell_b": round(2 * math.sqrt(l2), 1),
            "ell_deg": round(math.degrees(ang), 1), "residuals": [round(v, 1) for v in res[-40:]]}


def rebuild(upto=None):
    obs = state["obs"] if upto is None else state["obs"][:upto]
    by = {}
    for o in obs:
        by.setdefault(o["cell"], []).append(o)
    towers = {}
    for cell, lst in by.items():
        t = fit_tower(lst)
        if t:
            t.update(band=lst[-1]["band"], pci=lst[-1]["pci"], min_ta=min(o["ta"] for o in lst), last_ts=lst[-1]["ts"])
            # convergence trail: estimate after every 5th observation
            trail = []
            for k in range(3, len(lst) + 1, max(1, len(lst) // 12)):
                e = fit_tower(lst[:k])
                if e:
                    trail.append([e["lat"], e["lon"]])
            trail.append([t["lat"], t["lon"]])
            t["trail"] = trail
            if state["truth"]:
                near = min(state["truth"], key=lambda tt: math.hypot(*enu(tt["lat"], tt["lon"], t["lat"], t["lon"])))
                t["err_m"] = round(math.hypot(*enu(near["lat"], near["lon"], t["lat"], t["lon"])), 1)
        towers[cell] = t
    return towers


def add_obs(o):
    with lock:
        state["obs"].append(o)
        state["towers"] = rebuild()
    with open(OBS_CSV, "a", newline="") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS)
        if f.tell() == 0:
            w.writeheader()
        w.writerow({k: o.get(k) for k in FIELDS})


# ----------------------------------------------------------------------------- feeders
def sim_feed(speed):
    """Drive a loop through a small town with three towers; TA quantised, NLOS bias, occasional handover."""
    lat0, lon0 = 48.137, 11.575
    towers = [{"cell": "B3/85", "band": "B3", "pci": 85, "e": 900, "n": 600}, {"cell": "B3/38", "band": "B3", "pci": 38, "e": -1100, "n": 300},
              {"cell": "B8/171", "band": "B8", "pci": 171, "e": 200, "n": -1300}]
    state["truth"] = [{"cell": t["cell"], **dict(zip(("lat", "lon"), lla(t["e"], t["n"], lat0, lon0)))} for t in towers]
    state["source"] = "simulation"
    t = 0.0; ts = time.time()
    while True:
        # figure-8 route, ~2.5 km across, 14 m/s
        e = 1300 * math.sin(t / 90.0); n = 900 * math.sin(t / 45.0)
        lat, lon = lla(e + random.gauss(0, 3), n + random.gauss(0, 3), lat0, lon0)
        # serve the strongest tower (path loss ~ 1/d^3.5 with shadowing)
        best = None
        for tw in towers:
            d = math.hypot(e - tw["e"], n - tw["n"])
            p = -60 - 35 * math.log10(max(d, 30) / 100) + random.gauss(0, 3)
            if best is None or p > best[1]:
                best = (tw, p, d)
        tw, rsrp, d = best
        nlos = abs(random.gauss(0, 25)) + (60 if random.random() < 0.1 else 0)
        ta = int((d + nlos) // TA_M)
        state["pos"] = {"lat": lat, "lon": lon, "src": "sim"}
        add_obs({"ts": round(ts + t, 1), "lat": lat, "lon": lon, "cell": tw["cell"], "band": tw["band"], "pci": tw["pci"], "ta": ta, "rsrp": round(rsrp, 1)})
        t += 2.0
        time.sleep(2.0 / speed)


def replay_feed(path, speed):
    state["source"] = f"replay {os.path.basename(path)}"
    rows = list(csv.DictReader(open(path)))
    prev = None
    for r in rows:
        o = {"ts": float(r["ts"]), "lat": float(r["lat"]), "lon": float(r["lon"]), "cell": r["cell"], "band": r.get("band"),
             "pci": float(r["pci"]) if r.get("pci") else None, "ta": int(float(r["ta"])), "rsrp": float(r["rsrp"]) if r.get("rsrp") else None}
        if prev is not None:
            time.sleep(max(0, min(5, (o["ts"] - prev) / speed)))
        prev = o["ts"]; add_obs(o)
    state["status"] = "replay finished"


# ----------------------------------------------------------------------------- diag feeder
def _crc16(d):
    c = 0xFFFF
    for b in d:
        c ^= b
        for _ in range(8):
            c = (c >> 1) ^ 0x8408 if c & 1 else c >> 1
    return c ^ 0xFFFF


def _hdlc(p):
    d = p + bytes([_crc16(p) & 0xFF, _crc16(p) >> 8]); o = bytearray()
    for b in d:
        if b in (0x7E, 0x7D):
            o += bytes([0x7D, b ^ 0x20])
        else:
            o.append(b)
    return bytes(o) + b"\x7e"


def _unesc(f):
    o = bytearray(); e = False
    for b in f:
        if e:
            o.append(b ^ 0x20); e = False
        elif b == 0x7D:
            e = True
        else:
            o.append(b)
    return bytes(o)


def _log_mask(items, equip=0x0B, last=0xA00):
    mask = bytearray((last + 7) // 8)
    for it in items:
        mask[(it & 0xFFF) // 8] |= 1 << ((it & 0xFFF) % 8)
    return _hdlc(bytes([0x73, 0, 0, 0, 3, 0, 0, 0, equip, 0, 0, 0]) + last.to_bytes(4, "little") + bytes(mask))


def parse_b062_rach(body):
    """LTE MAC RACH Attempt → TA from Msg2 (RAR), 16 Ts units, or None."""
    import struct
    if len(body) < 8:
        return None
    pos = 4
    for _ in range(body[1]):
        if pos + 4 > len(body):
            break
        sid, sver, size = body[pos], body[pos + 1], struct.unpack("<H", body[pos + 2:pos + 4])[0]
        sp = body[pos + 4:pos + 4 + size]; pos += 4 + size
        if sid != 0x06:
            continue
        hdr = 4 if sver == 2 else 6
        if len(sp) < hdr:
            continue
        result, bitmask = sp[hdr - 3], sp[hdr - 1]
        q = hdr
        if bitmask & 1:
            q += 7 if sver == 0x32 else 4
        if bitmask & 2 and q + 7 <= len(sp):
            backoff, res2, tc_rnti, ta = struct.unpack("<HBHH", sp[q:q + 7])
            if ta <= 1282:
                return ta
    return None


def parse_b063_ta_cmds(body):
    """LTE MAC DL Transport Block (v0x31/0x32) → list of TA Command CE values (0..63, 31 = no change)."""
    import struct
    cmds = []
    if len(body) < 8 or body[0] not in (0x31, 0x32):
        return cmds
    num_tb = struct.unpack("<H", body[4:6])[0]; pos = 8 + (19 * 28 if body[0] == 0x31 else 0)
    for _ in range(num_tb):
        if pos + 16 > len(body):
            break
        size, npad, v1, cch, nsdu, hlen = struct.unpack("<LLLBBH", body[pos:pos + 16]); pos += 16
        for _ in range(nsdu):
            if pos + 3 > len(body):
                break
            v = int.from_bytes(body[pos:pos + 3], "little"); pos += 3
            is_mce, lcid = v & 1, (v >> 1) & 0x3F
            b = body[pos:pos + 9]; pos += 9
            if is_mce and lcid == 29 and b:
                cmds.append(b[0] & 0x3F)
            elif not is_mce and len(b) >= 9:
                npg, ndyn = struct.unpack("<5xBHx", b); pos += ndyn * 4
                for _ in range(npg):
                    more = 1
                    while more == 1 and pos + 4 <= len(body):
                        more = int.from_bytes(body[pos:pos + 4], "little") & 1; pos += 4
    return cmds


_cleanup = []


def diag_feed(host, port=2500):
    """Timing advance from the modem's Qualcomm diag stream, delivered by the Mudi's own diag-router over TCP.

    The stock diag-router (which only serves the USB diag port) is stopped for the session and one that streams
    to this machine is started instead; both are restored on exit. Log 0xB062 (RACH) gives the absolute TA, log
    0xB063 (MAC DL transport blocks) carries the Timing Advance Command CEs that update it (TA += cmd - 31).
    A re-registration is forced at start so an absolute TA arrives within seconds."""
    import socket, subprocess as sp
    ssh = ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=5", host]
    state["source"] = f"diag via {host}"
    # this machine's address on the Mudi network
    r = sh(["route", "-n", "get", host.split("@")[-1]], timeout=5)
    ifc = next((ln.split(":")[1].strip() for ln in r.splitlines() if "interface:" in ln), None)
    my_ip = next((ln.split()[1] for ln in sh(["ifconfig", ifc or "en12"], timeout=5).splitlines() if ln.strip().startswith("inet ")), None)
    if not my_ip:
        state["status"] = "diag: cannot determine my IP towards the Mudi"; return
    srv = socket.socket(); srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1); srv.bind(("0.0.0.0", port)); srv.listen(1); srv.settimeout(30)

    def restore():
        sh(ssh + ["killall diag-router 2>/dev/null; sleep 1; /etc/init.d/diag-router.init start >/dev/null 2>&1; echo restored"], timeout=20)
    _cleanup.append(restore)
    sh(ssh + ["/etc/init.d/diag-router.init stop >/dev/null 2>&1; killall diag-router 2>/dev/null; sleep 1; "
              f"(diag-router -s {my_ip}:{port} -d 0 >/tmp/diag-ta.log 2>&1 &); echo started"], timeout=20)
    try:
        conn, _ = srv.accept()
    except socket.timeout:
        state["status"] = "diag: router did not connect (is diag-router present? is my IP reachable from it?)"; restore(); return
    conn.settimeout(2)
    conn.sendall(_log_mask([0x062, 0x063, 0x0C2]))
    state["status"] = "diag connected; forcing re-registration for an initial TA…"
    ta = {"abs": None, "cmds": 0, "rach": 0, "ts": None}

    def reregister():
        sh(ssh + ["timeout 8 atcmd 'AT+COPS=2'; sleep 2; timeout 8 atcmd 'AT+COPS=0'"], timeout=30)
    threading.Thread(target=reregister, daemon=True).start()

    def sampler():   # GPS + serving cell every 2 s → one observation per sample once TA is absolute
        while True:
            out = sh(ssh + ["timeout 4 atcmd 'AT+QGPSLOC=2'; timeout 4 atcmd 'AT+QENG=\"servingcell\"'"], timeout=14)
            lat = lon = None; cell = band = None; pci = rsrp = None
            for ln in out.splitlines():
                if ln.startswith("+QGPSLOC:"):
                    q = ln.split(":", 1)[1].split(",")
                    try: lat, lon = float(q[1]), float(q[2])
                    except Exception: pass
                if ln.startswith('+QENG: "LTE"'):
                    q = [x.strip('"') for x in ln.split(":", 1)[1].split(",")]
                    try: band = "B" + q[7]; pci = float(q[5]); rsrp = float(q[11]); cell = f"{band}/{int(pci)}"
                    except Exception: pass
            src = "modem"
            if lat is None and state["browser_pos"] and time.time() - state["browser_pos"]["t"] < 15:
                lat, lon, src = state["browser_pos"]["lat"], state["browser_pos"]["lon"], "browser"
            if lat is not None:
                state["pos"] = {"lat": lat, "lon": lon, "src": src}
            st = f"TA {ta['abs']} ({ta['rach']} RACH, {ta['cmds']} TA cmds) · cell {cell} · " + (f"GPS {src}" if lat is not None else "no GPS fix (modem); allow browser location as fallback")
            state["status"] = st
            if ta["abs"] is not None and lat is not None and cell:
                add_obs({"ts": round(time.time(), 1), "lat": lat, "lon": lon, "cell": cell, "band": band, "pci": pci, "ta": ta["abs"], "rsrp": rsrp})
            time.sleep(2)
    threading.Thread(target=sampler, daemon=True).start()

    def keepalive():   # TA is only maintained while RRC-connected: keep a trickle of traffic on the Mudi link
        while True:
            sh(["ping", "-c", "8", "-i", "1", "-b", ifc or "en12", "8.8.8.8"], timeout=15)
    threading.Thread(target=keepalive, daemon=True).start()

    import struct
    buf = b""
    while True:
        try:
            d = conn.recv(65536)
        except socket.timeout:
            continue
        if not d:
            state["status"] = "diag: stream ended"; break
        buf += d
        while b"\x7e" in buf:
            fr, _, buf = buf.partition(b"\x7e")
            if len(fr) < 20:
                continue
            f = _unesc(fr)
            if f[0] == 0x98:
                f = f[8:]
            if f[0] != 0x10 or len(f) < 18:
                continue
            code = struct.unpack("<H", f[6:8])[0]; body = f[16:-2]
            if code == 0xB062:
                v = parse_b062_rach(body)
                if v is not None:
                    ta["abs"] = v; ta["rach"] += 1; ta["ts"] = time.time()
            elif code == 0xB063 and ta["abs"] is not None:
                for c in parse_b063_ta_cmds(body):
                    ta["cmds"] += 1; ta["abs"] = max(0, min(1282, ta["abs"] + (c - 31)))
    restore()


# ----------------------------------------------------------------------------- web
HTML = r"""<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Timing-advance tower locator</title>
<link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/leaflet/1.9.4/leaflet.min.css">
<script src="https://cdnjs.cloudflare.com/ajax/libs/leaflet/1.9.4/leaflet.min.js"></script>
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=IBM+Plex+Mono:wght@400;600&family=IBM+Plex+Sans:wght@400;600&display=swap">
<style>
 html,body{margin:0;height:100%;background:#07090c;color:#dfe6ea;font:14px "IBM Plex Sans",system-ui,sans-serif}
 #map{position:absolute;inset:0}
 .dark-tiles{filter:invert(1) hue-rotate(180deg) brightness(.85) saturate(.45) contrast(.95)}
 .leaflet-container{background:#07090c}
 .card{position:absolute;z-index:1000;background:#0d1117ee;border:1px solid #1f2933;border-radius:12px;padding:12px 14px;backdrop-filter:blur(6px)}
 #hud{top:12px;left:12px;width:340px}
 #towers{top:12px;right:12px;width:330px;max-height:80vh;overflow:auto}
 h1{font-size:15px;margin:0 0 2px;font-weight:600;letter-spacing:.2px}
 .dim{color:#7d8b96}.mono{font-family:"IBM Plex Mono",ui-monospace,monospace;font-variant-numeric:tabular-nums}
 .k{display:flex;justify-content:space-between;gap:10px;margin:3px 0}.k span:last-child{font-weight:600}
 .tw{border-top:1px solid #1f2933;padding:10px 0 6px;cursor:pointer}.tw:hover{background:#111820}
 .tw .name{font-weight:600;display:flex;justify-content:space-between}
 .sw{display:inline-block;width:10px;height:10px;border-radius:50%;margin-right:6px;vertical-align:-1px}
 .res{display:flex;align-items:center;gap:1px;height:26px;margin-top:6px}.res i{display:block;width:5px;background:#3b82f6;opacity:.8}
 input[type=range]{width:100%;margin:8px 0 2px}
 button{background:#1c2733;color:#dfe6ea;border:0;border-radius:6px;padding:5px 9px;margin-right:6px;cursor:pointer}button.on{background:#2b6d4f}
 .legend{font-size:12px;color:#7d8b96;margin-top:8px;line-height:1.5}
 @keyframes pulse{0%{r:6}50%{r:9}100%{r:6}}
</style></head><body>
<div id="map"></div>
<div id="hud" class="card">
 <h1>Timing-advance tower locator</h1>
 <div class="dim" id="src">–</div>
 <div class="k"><span>observations</span><span class="mono" id="nobs">0</span></div>
 <div class="k"><span>towers fitted</span><span class="mono" id="ntw">0</span></div>
 <div class="k"><span>showing</span><span class="mono" id="upto">live</span></div>
 <input type="range" id="scrub" min="0" max="0" value="0">
 <div><button id="live" class="on">live</button><button id="play">replay ▶</button><button id="rings" class="on">rings</button><button id="trail" class="on">convergence</button></div>
 <div class="legend">Each faint ring is one observation: the tower lies inside a 78 m band at TA·78 m from that point. Where the bands of one cell overlap, the fit converges; the ellipse is its 2σ uncertainty. Newest ring is brightest.</div>
</div>
<div id="towers" class="card"><b>Towers</b> <span class="dim">(click to zoom)</span><div id="tl"></div></div>
<script>
const map=L.map('map',{zoomControl:false}).setView([48.137,11.575],14); L.control.zoom({position:'bottomright'}).addTo(map); L.control.scale({imperial:false}).addTo(map);
L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png',{maxZoom:19,attribution:'© OpenStreetMap',className:'dark-tiles'}).addTo(map);
const ringL=L.layerGroup().addTo(map), pathL=L.layerGroup().addTo(map), towL=L.layerGroup().addTo(map), truthL=L.layerGroup().addTo(map);
const HUES=[200,150,30,280,330,90]; const hue={}; let nh=0; function H(c){ if(!(c in hue))hue[c]=HUES[nh++%HUES.length]; return hue[c]; }
let live=true, upto=null, showRings=true, showTrail=true, playing=false, fitted=false, timer=null, me=null;
if(navigator.geolocation) navigator.geolocation.watchPosition(p=>fetch('/api/pos',{method:'POST',body:JSON.stringify({lat:p.coords.latitude,lon:p.coords.longitude})}),null,{enableHighAccuracy:true});
function ellipse(lat,lon,a,b,deg,color){ const pts=[]; const cl=Math.cos(lat*Math.PI/180); for(let i=0;i<=48;i++){const t=i/48*2*Math.PI; const x=a*Math.cos(t), y=b*Math.sin(t); const th=deg*Math.PI/180; const e=x*Math.cos(th)-y*Math.sin(th), n=x*Math.sin(th)+y*Math.cos(th); pts.push([lat+n/111320, lon+e/(111320*cl)]);} return L.polygon(pts,{color,weight:1.5,fillColor:color,fillOpacity:.12,dashArray:'4 4'}); }
async function tick(){
 const st=await (await fetch('/api/state'+(upto!=null?'?upto='+upto:''))).json();
 document.getElementById('src').textContent=st.source+(st.status&&st.status!=='ok'?' · '+st.status:'');
 document.getElementById('nobs').textContent=st.n_total; document.getElementById('ntw').textContent=Object.keys(st.towers).length;
 const sc=document.getElementById('scrub'); sc.max=st.n_total; if(live){sc.value=st.n_total;} document.getElementById('upto').textContent=live?'live':(sc.value+' / '+st.n_total);
 ringL.clearLayers(); pathL.clearLayers(); towL.clearLayers(); truthL.clearLayers();
 const obs=st.obs; const N=obs.length;
 if(N){ pathL.addLayer(L.polyline(obs.map(o=>[o.lat,o.lon]),{color:'#9fb3c8',weight:1.5,opacity:.5}));
   const last=obs[N-1]; pathL.addLayer(L.circleMarker([last.lat,last.lon],{radius:6,color:'#fff',weight:2,fillColor:'#38bdf8',fillOpacity:1}));
   if(showRings){ const start=Math.max(0,N-90); for(let i=start;i<N;i++){ const o=obs[i]; const age=(N-1-i)/(N-start); const op=.55*(1-age)+.06; const c=`hsl(${H(o.cell)},80%,60%)`;
     const r=o.ta*78.12+39; ringL.addLayer(L.circle([o.lat,o.lon],{radius:r,color:c,weight:i===N-1?2:1,opacity:op,fill:false}));
     if(i===N-1){ ringL.addLayer(L.circle([o.lat,o.lon],{radius:r-39,color:c,weight:1,opacity:.5,fill:false,dashArray:'2 6'})); ringL.addLayer(L.circle([o.lat,o.lon],{radius:r+39,color:c,weight:1,opacity:.5,fill:false,dashArray:'2 6'})); } } }
   if(!fitted){map.fitBounds(L.latLngBounds(obs.map(o=>[o.lat,o.lon])).pad(.3)); fitted=true;} }
 if(st.pos){ if(!me){ me=L.circleMarker([st.pos.lat,st.pos.lon],{radius:7,color:'#fff',weight:2,fillColor:'#38bdf8',fillOpacity:1}).bindTooltip('you ('+st.pos.src+')').addTo(map); if(!fitted){map.setView([st.pos.lat,st.pos.lon],15);fitted=true;} } else me.setLatLng([st.pos.lat,st.pos.lon]); }
 (st.truth||[]).forEach(t=>truthL.addLayer(L.marker([t.lat,t.lon],{icon:L.divIcon({className:'',html:`<div style="color:#fff;font-size:18px;text-shadow:0 0 6px #000">✛</div>`})}).bindTooltip('true tower '+t.cell)));
 const tl=document.getElementById('tl'); tl.innerHTML='';
 Object.entries(st.towers).forEach(([cell,t])=>{ const c=`hsl(${H(cell)},80%,60%)`;
   if(showTrail&&t.trail.length>1) towL.addLayer(L.polyline(t.trail,{color:c,weight:1.5,opacity:.7,dashArray:'1 5'}));
   towL.addLayer(ellipse(t.lat,t.lon,t.ell_a,t.ell_b,t.ell_deg,c));
   towL.addLayer(L.marker([t.lat,t.lon],{icon:L.divIcon({className:'',iconAnchor:[12,12],html:`<svg width="24" height="24" viewBox="0 0 24 24"><circle cx="12" cy="12" r="6" fill="${c}" opacity=".9"><animate attributeName="r" values="5;9;5" dur="2s" repeatCount="indefinite"/><animate attributeName="opacity" values=".9;.4;.9" dur="2s" repeatCount="indefinite"/></circle><circle cx="12" cy="12" r="2.5" fill="#fff"/></svg>`})}).bindTooltip(`${cell} · ${t.n} obs · rms ${t.rms_m} m`+(t.err_m!=null?` · error ${t.err_m} m`:'')));
   const d=document.createElement('div'); d.className='tw'; const mx=Math.max(80,...t.residuals.map(Math.abs));
   d.innerHTML=`<div class="name"><span><span class="sw" style="background:${c}"></span>${cell}</span><span class="mono dim">${t.n} obs</span></div>
     <div class="k"><span class="dim">uncertainty 2σ</span><span class="mono">${t.ell_a} × ${t.ell_b} m</span></div>
     <div class="k"><span class="dim">range residual rms</span><span class="mono">${t.rms_m} m</span></div>
     ${t.err_m!=null?`<div class="k"><span class="dim">error vs truth</span><span class="mono" style="color:${t.err_m<100?'#4ade80':t.err_m<250?'#fbbf24':'#f87171'}">${t.err_m} m</span></div>`:''}
     <div class="k"><span class="dim">closest TA seen</span><span class="mono">${t.min_ta} (≤ ${Math.round((t.min_ta+1)*78)} m)</span></div>
     <div class="res">${t.residuals.map(r=>`<i style="height:${Math.max(2,Math.abs(r)/mx*26)}px;background:${r>0?'#60a5fa':'#f472b6'}"></i>`).join('')}</div>`;
   d.onclick=()=>map.setView([t.lat,t.lon],16); tl.appendChild(d); });
}
document.getElementById('scrub').oninput=e=>{live=false;document.getElementById('live').classList.remove('on');upto=+e.target.value;tick();};
document.getElementById('live').onclick=e=>{live=true;upto=null;e.target.classList.add('on');playing=false;tick();};
document.getElementById('play').onclick=e=>{ playing=!playing; e.target.classList.toggle('on',playing); if(playing){live=false;document.getElementById('live').classList.remove('on'); upto=3; const sc=document.getElementById('scrub'); timer=setInterval(()=>{ if(!playing){clearInterval(timer);return;} upto=Math.min(+sc.max,upto+2); sc.value=upto; tick(); if(upto>=+sc.max){playing=false;e.target.classList.remove('on');clearInterval(timer);} },150);} };
document.getElementById('rings').onclick=e=>{showRings=!showRings;e.target.classList.toggle('on',showRings);tick();};
document.getElementById('trail').onclick=e=>{showTrail=!showTrail;e.target.classList.toggle('on',showTrail);tick();};
setInterval(()=>{ if(live) tick(); },2000); tick();
</script></body></html>"""


class H(BaseHTTPRequestHandler):
    def log_message(self, *a): pass

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0); raw = self.rfile.read(n) if n else b""
        if self.path == "/api/pos":
            try:
                q = json.loads(raw); state["browser_pos"] = {"lat": float(q["lat"]), "lon": float(q["lon"]), "t": time.time()}
            except Exception:
                pass
        body = b'{"ok":true}'
        self.send_response(200); self.send_header("Content-Type", "application/json"); self.send_header("Content-Length", str(len(body))); self.end_headers(); self.wfile.write(body)

    def do_GET(self):
        if self.path.startswith("/api/state"):
            upto = None
            if "upto=" in self.path:
                try: upto = int(self.path.split("upto=")[1].split("&")[0])
                except Exception: pass
            with lock:
                obs = state["obs"] if upto is None else state["obs"][:upto]
                towers = state["towers"] if upto is None else rebuild(upto)
                body = json.dumps({"obs": obs[-600:], "n_total": len(state["obs"]), "towers": towers, "truth": state["truth"],
                                   "source": state["source"], "status": state["status"], "pos": state["pos"]}).encode()
            ct = "application/json"
        else:
            body = HTML.encode(); ct = "text/html; charset=utf-8"
        self.send_response(200); self.send_header("Content-Type", ct); self.send_header("Content-Length", str(len(body))); self.end_headers(); self.wfile.write(body)


def main():
    ap = argparse.ArgumentParser()
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--sim", action="store_true"); g.add_argument("--replay", metavar="CSV"); g.add_argument("--diag", metavar="HOST", nargs="?", const="root@192.168.8.1")
    ap.add_argument("--speed", type=float, default=1.0, help="replay/sim speed factor")
    ap.add_argument("--port", type=int, default=8766)
    a = ap.parse_args()
    if a.sim:
        threading.Thread(target=sim_feed, args=(a.speed,), daemon=True).start()
    elif a.replay:
        threading.Thread(target=replay_feed, args=(a.replay, a.speed), daemon=True).start()
    else:
        threading.Thread(target=diag_feed, args=(a.diag,), daemon=True).start()
    state["status"] = "ok"
    print(f"open http://localhost:{a.port}   observations → {OBS_CSV}")
    import signal
    def bye(*_):
        for fn in _cleanup:
            try: fn()
            except Exception: pass
        os._exit(0)
    signal.signal(signal.SIGTERM, bye); signal.signal(signal.SIGINT, bye)
    ThreadingHTTPServer(("0.0.0.0", a.port), H).serve_forever()


if __name__ == "__main__":
    main()
