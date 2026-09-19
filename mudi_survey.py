#!/usr/bin/env python3
"""Drive survey for the Mudi 7: where is upload fastest?

Every 2 s it pulls from the Mudi's modem (over ssh root@192.168.8.1):
  * GPS fix (modem GNSS, AT+QGPSLOC) — or the browser's geolocation if you open the page on a phone
  * serving cell: RAT (LTE / NR5G-NSA / NR5G-SA), bands, bandwidths, PCI, RSRP, RSRQ, SINR
  * carrier aggregation summary (AT+QCAINFO), e.g. B3(20)+B3(10)+B8(10)+n1(20)
and measures upload throughput on the Mudi link:
  * passive: bytes leaving the interface (if the big uploader is running, that's the real rate)
  * active probe every --probe-every s: an 8 MB PUT to storage.googleapis.com bound to the interface
Everything is appended to ~/mnemosyne-recon.csv and shown live on a map at http://localhost:8765
(also reachable from a phone on the Mudi Wi-Fi at http://<mac-ip>:8765).

Usage:  python3 ~/mnemosyne-recon.py [--iface en12] [--probe-every 20] [--probe-mb 8] [--port 8765] [--no-probe]
"""
import argparse, csv, json, math, os, re, subprocess, sys, threading, time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ROUTER = "root@192.168.8.1"
SSH = ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=4", "-o", "ControlMaster=auto",
       "-o", "ControlPath=/tmp/mnemosyne-recon-ssh-%r@%h", "-o", "ControlPersist=600", ROUTER]
CSV = os.path.expanduser("~/mnemosyne-recon.csv")
FIELDS = ["ts", "lat", "lon", "gps_src", "speed_kmh", "nsat", "rat", "lte_band", "lte_bw", "lte_pci", "lte_rsrp", "lte_rsrq",
          "lte_sinr", "nr_band", "nr_bw", "nr_pci", "nr_rsrp", "nr_sinr", "ca", "n_carriers", "ul_probe_mbps", "ul_passive_mbps", "note", "neighbours"]
LTE_BW = {6: 1.4, 15: 3, 25: 5, 50: 10, 75: 15, 100: 20}
QENG_BW = {0: 1.4, 1: 3, 2: 5, 3: 10, 4: 15, 5: 20}   # AT+QENG bandwidth codes
NR_BW = {0: 5, 1: 10, 2: 15, 3: 20, 4: 25, 5: 30, 6: 40, 7: 50, 8: 60, 9: 70, 10: 80, 11: 90, 12: 100}

state = {"latest": {}, "samples": [], "best": [], "masts": [], "status": "starting", "probe_busy": False, "browser_pos": None,
         "bandtest": {"running": False, "log": [], "results": []}, "scan": {"running": False, "cells": [], "raw": ""}}
LTE_BANDS = [(1, 0, 599), (3, 1200, 1949), (7, 2750, 3449), (8, 3450, 3799), (20, 6150, 6449), (28, 9210, 9659), (32, 9920, 10359),
             (38, 37750, 38249), (40, 38650, 39649), (41, 39650, 41589), (42, 41590, 43589), (43, 43590, 45589)]
NR_BANDS = [(28, 151600, 160600), (20, 158200, 164200), (8, 185000, 192000), (3, 361000, 376000), (1, 422000, 434000),
            (7, 524000, 538000), (38, 514000, 524000), (40, 460000, 480000), (41, 499200, 537999), (78, 620000, 653333), (77, 620000, 680000)]
DEFAULTS = {"lte_band": "1:3:5:7:8:20:28:32:38:40:41:42:43", "nsa_nr5g_band": "1:3:5:7:8:20:26:28:38:40:41:75:76:77:78",
            "nr5g_band": "1:3:5:7:8:20:26:28:38:40:41:75:76:77:78", "nr5g_disable_mode": "0"}


def earfcn_band(f, nr=False):
    for b, lo, hi in (NR_BANDS if nr else LTE_BANDS):
        if lo <= f <= hi:
            return ("n" if nr else "B") + str(b)
    return ("n?" if nr else "B?")
lock = threading.Lock()
args = None


def sh(cmd, timeout=12):
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout).stdout
    except Exception:
        return ""


def router(at_cmds):
    """Run several AT commands on the Mudi in one ssh round-trip; returns combined output."""
    script = "; ".join(f"timeout 4 atcmd '{c}'" for c in at_cmds)
    return sh(SSH + [script], timeout=14)


def num(x):
    try:
        return float(x)
    except Exception:
        return None


# AT+QCAINFO moves its fields between RATs and firmware variants, so every shape
# is read by its own map rather than by shared offsets: the 8-field NR line puts
# the PCI where the other shapes put a spare field, and the 12-field NR line
# pushes RSRP/RSRQ four places further right.
#   (RAT, field count): (arfcn, bandwidth, pci, rsrp, rsrq)
QCAINFO_FIELDS = {("LTE", 10): (1, 2, 5, 6, 7),
                  ("LTE", 13): (1, 2, 5, 6, 7),
                  ("NR5G", 8): (1, 2, 4, 5, 6),
                  ("NR5G", 12): (1, 2, 5, 9, 10)}
# Shapes we have not seen still carry band and channel in their first fields.
QCAINFO_UNKNOWN = (1, 2, None, None, None)


def parse_carriers(out):
    """Every aggregated carrier from AT+QCAINFO, keeping the per-carrier signal.

    SINR is deliberately not read here. The serving carrier reports it through
    QENG, secondary carriers do not report one at all, and the field sitting
    where SINR would go holds unvalidated values (745, -32768) that are not dB.
    """
    carriers = []
    for ln in out.splitlines():
        ln = ln.strip()
        if not ln.startswith("+QCAINFO:"):
            continue
        p = [x.strip().strip('"') for x in ln.split(":", 1)[1].split(",")]
        m = re.match(r"(LTE|NR5G) BAND (\d+)", p[3]) if len(p) > 3 else None
        if not m:
            continue
        nr = m.group(1) == "NR5G"
        arfcn, bw, pci, rsrp, rsrq = (num(p[i]) if i is not None and i < len(p) else None
                                      for i in QCAINFO_FIELDS.get((m.group(1), len(p)), QCAINFO_UNKNOWN))
        width = (NR_BW if nr else LTE_BW).get(int(bw or 0), bw or 0)
        carriers.append({"role": p[0], "rat": "nr" if nr else "lte",
                         "band": ("n" if nr else "B") + m.group(2), "arfcn": arfcn,
                         "bw_mhz": width, "pci": pci, "rsrp": rsrp, "rsrq": rsrq})
    return carriers


def parse_modem(out):
    d = {"rat": None, "lte_band": None, "lte_bw": None, "lte_pci": None, "lte_rsrp": None, "lte_rsrq": None, "lte_sinr": None,
         "nr_band": None, "nr_bw": None, "nr_pci": None, "nr_rsrp": None, "nr_sinr": None, "ca": "", "n_carriers": 0,
         "lat": None, "lon": None, "speed_kmh": None, "nsat": None, "gps_src": None, "neighbours": []}
    for ln in out.splitlines():
        ln = ln.strip()
        if ln.startswith('+QENG: "LTE"'):
            p = [x.strip('"') for x in ln.split(":", 1)[1].split(",")]
            # "LTE",mode,MCC,MNC,cellID,PCID,EARFCN,band,UL_bw,DL_bw,TAC,RSRP,RSRQ,RSSI,SINR,CQI,txpwr,srxlev
            if len(p) >= 15:
                d.update(rat=d["rat"] or "LTE", lte_band="B" + p[7], lte_bw=QENG_BW.get(int(num(p[9]) or 0), num(p[9])),
                         lte_pci=num(p[5]), lte_rsrp=num(p[11]), lte_rsrq=num(p[12]), lte_sinr=num(p[14]))
        elif ln.startswith('+QENG: "NR5G-NSA"'):
            p = [x.strip('"') for x in ln.split(":", 1)[1].split(",")]
            # "NR5G-NSA",MCC,MNC,PCID,RSRP,SINR,RSRQ,ARFCN,band,DL_bw,scs
            if len(p) >= 10:
                d.update(rat="NR5G-NSA")
                # A leg carrying no measurement yet reports PCI 65535 (0xFFFF),
                # band 0 and dashes. The RAT is real, so keep it; the identity is
                # not, and recording it invents a cell "n0" no network transmits.
                if num(p[3]) != 65535 and p[8] != "0":
                    d.update(nr_pci=num(p[3]), nr_rsrp=num(p[4]), nr_sinr=num(p[5]), nr_band="n" + p[8],
                             nr_bw=NR_BW.get(int(num(p[9]) or 0), num(p[9])))
        elif ln.startswith('+QENG: "NR5G-SA"'):
            p = [x.strip('"') for x in ln.split(":", 1)[1].split(",")]
            # "NR5G-SA",duplex,MCC,MNC,cellID,PCID,TAC,ARFCN,band,DL_bw,RSRP,RSRQ,SINR,scs,srxlev
            if len(p) >= 13:
                d.update(rat="NR5G-SA", nr_pci=num(p[5]), nr_band="n" + p[8], nr_bw=NR_BW.get(int(num(p[9]) or 0), num(p[9])),
                         nr_rsrp=num(p[10]), nr_sinr=num(p[12]))
        elif ln.startswith('+QENG: "neighbourcell'):
            p = [x.strip('"') for x in ln.split(":", 1)[1].split(",")]
            # "neighbourcell intra|inter","LTE",EARFCN,PCI,RSRQ,RSRP,RSSI,...
            if len(p) >= 6 and p[1] == "LTE" and num(p[2]) is not None:
                d["neighbours"].append({"kind": p[0].split()[-1], "earfcn": int(num(p[2])), "band": earfcn_band(int(num(p[2]))),
                                        "pci": num(p[3]), "rsrq": num(p[4]), "rsrp": num(p[5])})
        elif ln.startswith("+QGPSLOC:"):
            p = ln.split(":", 1)[1].split(",")
            # UTC,lat,lon,hdop,alt,fix,cog,spkm,spkn,date,nsat   (mode 2: decimal degrees)
            if len(p) >= 11 and num(p[1]) is not None:
                d.update(lat=num(p[1]), lon=num(p[2]), speed_kmh=num(p[7]), nsat=num(p[10]), gps_src="modem")
    # The carrier list is the primary record; the CA string is a rendering of it.
    d["carriers"] = parse_carriers(out)
    d["ca"] = "+".join(f'{c["band"]}({c["bw_mhz"]:g})' for c in d["carriers"])
    d["n_carriers"] = len(d["carriers"])
    return d


def passive_rate(iface, prev):
    out = sh(["netstat", "-ibn", "-I", iface], timeout=3).splitlines()
    ob = None
    for ln in out[1:]:
        p = ln.split()
        if len(p) >= 10 and p[9].isdigit():
            ob = int(p[9]); break
    now = time.time()
    rate = None
    if ob is not None and prev[0] is not None and now > prev[1]:
        rate = (ob - prev[0]) * 8 / 1e6 / (now - prev[1])
    return rate, (ob, now)


def probe(iface, mb):
    """Active upload probe bound to iface: PUT mb MB to GCS (server answers 400; speed is what we want)."""
    tmp = f"/tmp/mnemosyne-probe-{mb}mb.bin"
    if not os.path.exists(tmp):
        with open(tmp, "wb") as f:
            f.write(os.urandom(mb * 1048576))
    # pin the address: macOS caches a failed lookup for a while after every modem re-registration
    ip = (sh(["dig", "+short", "+time=2", "storage.googleapis.com", "@8.8.8.8"], timeout=6).split() or ["142.250.185.187"])[-1]
    out = sh(["curl", "-s", "-o", "/dev/null", "-m", "12", "--interface", iface, "--resolve", f"storage.googleapis.com:443:{ip}", "-T", tmp,
              "-w", "%{speed_upload} %{time_total} %{http_code}", "https://storage.googleapis.com/"], timeout=15)
    p = out.split()
    if len(p) >= 2 and num(p[0]):
        return num(p[0]) * 8 / 1e6
    return None


def grid_key(lat, lon, m=60):
    return (round(lat / (m / 111320.0)), round(lon / (m / (111320.0 * math.cos(math.radians(lat))))))


def rebuild_best():
    cells = {}
    for s in state["samples"]:
        if s.get("lat") is None:
            continue
        k = grid_key(s["lat"], s["lon"])
        c = cells.setdefault(k, {"lat": s["lat"], "lon": s["lon"], "n": 0, "probe_max": None, "passive_max": None, "sinr": [], "ca": s.get("ca"), "rat": s.get("rat"), "ts": s["ts"]})
        c["n"] += 1
        for key in ("probe_max", "passive_max"):
            v = s.get("ul_probe_mbps" if key == "probe_max" else "ul_passive_mbps")
            if v is not None and (c[key] is None or v > c[key]):
                c[key] = v; c["ca"] = s.get("ca"); c["rat"] = s.get("rat"); c["ts"] = s["ts"]; c["lat"] = s["lat"]; c["lon"] = s["lon"]
        if s.get("lte_sinr") is not None:
            c["sinr"].append(s["lte_sinr"])
    best = []
    for c in cells.values():
        score = max([v for v in (c["probe_max"], c["passive_max"]) if v is not None] or [-1])
        best.append({"lat": c["lat"], "lon": c["lon"], "n": c["n"], "ul_mbps": None if score < 0 else round(score, 1),
                     "sinr": round(sum(c["sinr"]) / len(c["sinr"]), 1) if c["sinr"] else None, "ca": c["ca"], "rat": c["rat"], "ts": c["ts"]})
    best.sort(key=lambda b: (b["ul_mbps"] if b["ul_mbps"] is not None else -1, b["sinr"] or -99), reverse=True)
    state["best"] = best[:25]


def rebuild_masts():
    acc = {}
    for s in state["samples"]:
        if s.get("lat") is None:
            continue
        obs = []
        if s.get("lte_pci") is not None and s.get("lte_rsrp") is not None:
            obs.append((f"{s.get('lte_band')}/{int(s['lte_pci'])}", s["lte_rsrp"]))
        for n in s.get("neighbours") or []:
            if n.get("pci") is not None and n.get("rsrp") is not None:
                obs.append((f"{n['band']}/{int(n['pci'])}", n["rsrp"]))
        for key, rsrp in obs:
            w = 10 ** (rsrp / 10.0)  # linear power: strongest readings dominate
            a = acc.setdefault(key, {"w": 0.0, "lat": 0.0, "lon": 0.0, "n": 0, "best": -999, "pts": []})
            a["w"] += w; a["lat"] += w * s["lat"]; a["lon"] += w * s["lon"]; a["n"] += 1; a["best"] = max(a["best"], rsrp); a["pts"].append((s["lat"], s["lon"], w))
    masts = []
    for key, a in acc.items():
        if a["n"] < 8 or a["w"] <= 0:
            continue
        lat, lon = a["lat"] / a["w"], a["lon"] / a["w"]
        spread = math.sqrt(sum(w * ((la - lat) * 111320) ** 2 + w * ((lo - lon) * 111320 * math.cos(math.radians(lat))) ** 2 for la, lo, w in a["pts"]) / a["w"])
        masts.append({"id": key, "lat": lat, "lon": lon, "n": a["n"], "best_rsrp": a["best"], "spread_m": round(spread)})
    state["masts"] = masts


def at(cmd, timeout=8):
    return sh(SSH + [f"timeout {timeout} atcmd '{cmd}'"], timeout=timeout + 6)


def wan_up():
    return '"up": true' in sh(SSH + ["ubus call network.interface.modem_cpu status"], timeout=8)


def reattach():
    at("AT+COPS=2", 15); time.sleep(2); at("AT+COPS=0", 15)


def wait_for(pred, secs):
    t0 = time.time()
    while time.time() - t0 < secs:
        d = parse_modem(at('AT+QENG="servingcell"'))
        if pred(d):
            return d
        time.sleep(2)
    return None


def bandtest_thread():
    bt = state["bandtest"]; bt["running"] = True; bt["log"] = []; bt["results"] = []
    def log(m): bt["log"].append(f"{time.strftime('%H:%M:%S')} {m}")
    try:
        cur = state["latest"]
        bands = sorted({b for b in [cur.get("lte_band")] + [n["band"] for n in cur.get("neighbours") or []] if b and b[1:].isdigit()}, key=lambda b: int(b[1:]))
        plan = [("LTE " + b, {"lte_band": b[1:]}, lambda d, b=b: d.get("lte_band") == b) for b in bands]
        plan += [("5G NSA n78", {"nsa_nr5g_band": "78"}, lambda d: d.get("rat") == "NR5G-NSA" and d.get("nr_band") == "n78"),
                 ("5G SA only", {"nr5g_disable_mode": "2"}, lambda d: d.get("rat") == "NR5G-SA")]
        log(f"plan: {', '.join(p[0] for p in plan)} (each ~30-45 s)")
        for name, prefs, ok in plan:
            log(f"{name}: locking…")
            for k, v in prefs.items():
                at(f'AT+QNWPREFCFG="{k}",{v}')
            reattach()
            d = wait_for(ok, 40)
            if not d:
                log(f"{name}: no service on this band here")
                bt["results"].append({"name": name, "mbps": None, "rsrp": None, "sinr": None, "ca": None, "note": "no service"})
            else:
                t0 = time.time()
                while time.time() - t0 < 40 and not wan_up():
                    time.sleep(3)
                time.sleep(3)
                mb = probe(args.iface, 4)
                mb2 = probe(args.iface, 4)
                best = max([x for x in (mb, mb2) if x is not None] or [None]) if (mb or mb2) else None
                bt["results"].append({"name": name, "mbps": None if best is None else round(best, 1), "rsrp": d.get("lte_rsrp") if "LTE" in name else d.get("nr_rsrp"),
                                      "sinr": d.get("lte_sinr") if "LTE" in name else d.get("nr_sinr"), "ca": d.get("ca"), "note": "" if best else "WAN not up / probe failed"})
                log(f"{name}: {bt['results'][-1]['mbps']} Mbps  {d.get('ca')}")
            for k in prefs:
                at(f'AT+QNWPREFCFG="{k}",{DEFAULTS[k]}')
    except Exception as e:
        log(f"error: {e}")
    finally:
        for k, v in DEFAULTS.items():
            at(f'AT+QNWPREFCFG="{k}",{v}')
        reattach(); wait_for(lambda d: d.get("rat") is not None, 40)
        log("restored defaults, back on " + str((state["latest"] or {}).get("rat")))
        bt["running"] = False


def scan_thread():
    sc = state["scan"]; sc["running"] = True; sc["cells"] = []; sc["raw"] = ""
    try:
        out = sh(SSH + ["timeout 150 atcmd 'AT+QSCAN=3'"], timeout=160)
        sc["raw"] = out
        for ln in out.splitlines():
            if ln.startswith("+QSCAN:"):
                p = [x.strip('"') for x in ln.split(":", 1)[1].split(",")]
                if len(p) >= 7 and num(p[3]) is not None:
                    nr = p[0].startswith("NR")
                    sc["cells"].append({"rat": "5G" if nr else "LTE", "plmn": p[1] + "-" + p[2], "freq": int(num(p[3])),
                                        "band": earfcn_band(int(num(p[3])), nr), "pci": num(p[4]), "rsrp": num(p[5]), "rsrq": num(p[6])})
        sc["cells"].sort(key=lambda c: c["rsrp"] if c["rsrp"] is not None else -999, reverse=True)
    finally:
        sc["running"] = False


def poller():
    new_csv = not os.path.exists(CSV)
    f = open(CSV, "a", newline="")
    w = csv.DictWriter(f, fieldnames=FIELDS)
    if new_csv:
        w.writeheader()
    prev = (None, 0.0)
    last_probe = 0.0
    last_probe_val = None
    router(["AT+QGPS=1"])  # enable GNSS (harmless if already on)
    while True:
        t0 = time.time()
        if state["bandtest"]["running"] or state["scan"]["running"]:
            time.sleep(2); continue
        out = router(['AT+QENG="servingcell"', "AT+QCAINFO", 'AT+QENG="neighbourcell"', "AT+QGPS?", "AT+QGPSLOC=2"])
        if "+QGPS: 0" in out:
            router(["AT+QGPS=1"])   # something (e.g. a re-registration) switched GNSS off: turn it back on
        d = parse_modem(out)
        rate, prev = passive_rate(args.iface, prev)
        if d["lat"] is None and state["browser_pos"] and time.time() - state["browser_pos"]["t"] < 120:
            bp = state["browser_pos"]
            d.update(lat=bp["lat"], lon=bp["lon"], speed_kmh=bp.get("speed_kmh"), gps_src="browser")
        note = ""
        if not out.strip():
            note = "router unreachable"
        elif "+CME ERROR: 516" in out or d["lat"] is None:
            note = "no GPS fix"
        pv = None
        if not args.no_probe and (time.time() - last_probe >= args.probe_every or state.get("probe_now")):
            state["probe_now"] = False; state["probe_busy"] = True
            pv = probe(args.iface, args.probe_mb)
            state["probe_busy"] = False; last_probe = time.time(); last_probe_val = pv
        s = {"ts": round(time.time(), 1), **d, "ul_probe_mbps": None if pv is None else round(pv, 1),
             "ul_passive_mbps": None if rate is None else round(rate, 1), "note": note}
        with lock:
            state["latest"] = {**s, "last_probe_mbps": None if last_probe_val is None else round(last_probe_val, 1)}
            state["samples"].append(s)
            if len(state["samples"]) > 20000:
                state["samples"] = state["samples"][-20000:]
            rebuild_best(); rebuild_masts()
            state["status"] = "ok" if not note else note
        row = {k: s.get(k) for k in FIELDS}
        row["neighbours"] = ";".join(f"{n['band']}/{int(n['pci'])}:{n['rsrp']:.0f}" for n in d["neighbours"] if n.get("pci") is not None)
        w.writerow(row); f.flush()
        time.sleep(max(0.2, 2.0 - (time.time() - t0)))


HTML = r"""<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Mudi drive survey</title>
<link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/leaflet/1.9.4/leaflet.min.css">
<script src="https://cdnjs.cloudflare.com/ajax/libs/leaflet/1.9.4/leaflet.min.js"></script>
<style>
 html,body{margin:0;height:100%;font:14px -apple-system,system-ui,sans-serif;background:#111;color:#eee}
 #map{position:absolute;inset:0}
 .card{position:absolute;z-index:1000;background:#161616;padding:12px 14px;border-radius:12px;box-shadow:0 4px 18px rgba(0,0,0,.5)}
 #panel{top:10px;left:10px;width:330px}
 #best{top:10px;right:10px;max-height:70vh;overflow:auto;min-width:300px}
 .big{font-size:52px;font-weight:800;line-height:1;letter-spacing:-1px}
 .unit{font-size:14px;color:#9a9a9a;font-weight:500;margin-left:6px}
 .dim{color:#9a9a9a}.g{color:#57d36b}.y{color:#ffd166}.r{color:#ff6b6b}
 .row{display:flex;justify-content:space-between;align-items:baseline;margin:5px 0;gap:8px}
 .lbl{color:#9a9a9a;font-size:12px;text-transform:uppercase;letter-spacing:.4px}
 .val{font-weight:600;text-align:right}
 .badge{display:inline-block;padding:2px 8px;border-radius:6px;background:#2b2b2b;font-weight:600}
 .bar{height:5px;background:#333;border-radius:3px;overflow:hidden;margin-top:3px}.bar i{display:block;height:100%}
 canvas{width:100%;height:44px;display:block;margin:8px 0 2px}
 table{border-collapse:collapse;width:100%}td,th{padding:3px 6px;text-align:right;white-space:nowrap}th{color:#9a9a9a;font-weight:500;font-size:12px}
 tr.spot{cursor:pointer}tr.spot:hover{background:#333}
 button{background:#333;color:#eee;border:0;border-radius:6px;padding:6px 10px;margin:6px 4px 0 0;cursor:pointer}button.on{background:#2a6}
 hr{border:0;border-top:1px solid #333;margin:8px 0}
 .leaflet-container{background:#222}
</style></head><body>
<div id="map"></div>
<div id="panel" class="card">
 <div><span class="big" id="ul">–</span><span class="unit">Mbps upload test</span></div>
 <div class="dim" id="ulsub" style="font-size:12px">waiting for first probe…</div>
 <canvas id="spark" width="600" height="88"></canvas>
 <div class="row"><span class="lbl">network</span><span class="val"><span class="badge" id="rat">–</span></span></div>
 <div class="row"><span class="lbl">upload carrier</span><span class="val" id="ulc">–</span></div>
 <div class="row"><span class="lbl">all carriers</span><span class="val dim" id="ca" style="font-weight:500">–</span></div>
 <div class="row"><span class="lbl">signal RSRP</span><span class="val" id="rsrp">–</span></div><div class="bar"><i id="rsrpb"></i></div>
 <div class="row"><span class="lbl">quality SINR</span><span class="val" id="sinr">–</span></div><div class="bar"><i id="sinrb"></i></div>
 <div class="row"><span class="lbl">5G carrier</span><span class="val" id="nr">–</span></div>
 <hr>
 <div class="dim" id="gps" style="font-size:12px">–</div>
 <div><button id="follow" class="on">follow</button><button id="probe">probe now</button><button id="color">color: upload</button><button id="center">center + zoom</button><button id="geo">browser GPS</button></div>
</div>
<div id="cells" class="card" style="bottom:12px;left:10px;width:330px;max-height:46vh;overflow:auto">
 <b>Cells heard here</b> <span class="dim" style="font-size:12px">(neighbour list, live)</span>
 <table id="ct"><thead><tr><th style="text-align:left">band</th><th>PCI</th><th>RSRP</th><th>RSRQ</th></tr></thead><tbody></tbody></table>
 <div style="margin-top:6px"><button id="bandtest">test bands here (parked, ~4 min)</button><button id="scan">full scan (parked, ~2 min)</button></div>
 <div id="btlog" class="dim" style="font-size:12px;margin-top:6px;white-space:pre-line"></div>
 <table id="btt" style="margin-top:4px"><tbody></tbody></table>
 <table id="sct" style="margin-top:4px"><tbody></tbody></table>
</div>
<div id="best" class="card"><b>Best spots</b> <span class="dim" style="font-size:12px">60 m cells, by best upload test</span>
 <div id="besthint" class="dim" style="margin-top:6px">waiting for a GPS fix, then drive…</div>
 <table id="bt"><thead><tr><th>#</th><th>Mbps</th><th>SINR</th><th style="text-align:left">upload carrier</th><th>when</th></tr></thead><tbody></tbody></table></div>
<script>
const map=L.map('map',{zoomControl:false}).setView([51.1,10.4],6); L.control.zoom({position:'bottomright'}).addTo(map); L.control.scale({imperial:false}).addTo(map);
L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png',{maxZoom:19,attribution:'© OpenStreetMap'}).addTo(map);
const layer=L.layerGroup().addTo(map);const bestLayer=L.layerGroup().addTo(map);const mastLayer=L.layerGroup().addTo(map);
let follow=true,colorBy='ul',drawn=0,me=null,geoWatch=null,hist=[];
function col(v,kind){ if(v==null)return '#666'; let t=kind==='ul'?Math.min(1,v/150):Math.max(0,Math.min(1,(v+5)/35)); return `hsl(${Math.round(120*t)},85%,50%)`; }
function fmt(v,d=0){return v==null?'–':(+v).toFixed(d)}
function cls(v,g,y){return v==null?'dim':v>=g?'g':v>=y?'y':'r'}
function pct(v,lo,hi){return Math.max(0,Math.min(100,100*(v-lo)/(hi-lo)))+'%'}
function pcc(ca){ if(!ca)return null; const f=ca.split('+')[0]; const m=f.match(/^([Bn]\d+)\((\d+(?:\.\d+)?)\)/); return m?{band:m[1],bw:+m[2]}:null; }
function spark(){ const c=document.getElementById('spark'),x=c.getContext('2d'); x.clearRect(0,0,c.width,c.height); const h=hist.slice(-40); if(h.length<2)return;
  const mx=Math.max(150,...h); const w=c.width/40; h.forEach((v,i)=>{ x.fillStyle=col(v,'ul'); const bh=v/mx*(c.height-6); x.fillRect(c.width-(h.length-i)*w+2,c.height-bh,w-3,bh); });
  x.fillStyle='#9a9a9a'; x.font='20px sans-serif'; x.fillText('last '+h.length+' tests', 4, 22); }
async function tick(){
 let st; try{st=await (await fetch('/api/state?since='+drawn)).json();}catch(e){document.getElementById('gps').textContent='server unreachable';return;}
 const l=st.latest||{}; const ul=l.last_probe_mbps, pv=l.ul_passive_mbps;
 const ulEl=document.getElementById('ul'); ulEl.textContent=ul==null?'–':ul.toFixed(0); ulEl.className='big '+cls(ul,80,40);
 document.getElementById('ulsub').textContent=(pv!=null&&pv>1?`link traffic now ${pv.toFixed(0)} Mbps · `:'')+(st.probe_busy?'probing…':'next probe soon');
 for(const s of st.samples) if(s.ul_probe_mbps!=null) hist.push(s.ul_probe_mbps); spark();
 const r=document.getElementById('rat'); r.textContent=(l.rat||'no cell').replace('NR5G-','5G '); r.style.background=l.rat==='NR5G-SA'?'#1d6b3a':l.rat==='NR5G-NSA'?'#1f4d7a':'#5a3d1a';
 const p=pcc(l.ca); const uc=document.getElementById('ulc'); uc.textContent=p?`${p.band} · ${p.bw} MHz`:(l.lte_band?`${l.lte_band} · ${fmt(l.lte_bw)} MHz`:'–'); uc.className='val '+(p?(p.bw>=20?'g':p.bw>=15?'y':'r'):'dim');
 document.getElementById('ca').textContent=l.ca?l.ca.replaceAll('+',' + '):'–';
 const rs=l.lte_rsrp; document.getElementById('rsrp').innerHTML=`<span class="${cls(rs,-80,-95)}">${fmt(rs)} dBm</span> <span class="dim" style="font-size:12px">PCI ${fmt(l.lte_pci)}</span>`; const rb=document.getElementById('rsrpb'); rb.style.width=rs==null?'0':pct(rs,-120,-60); rb.style.background=col(rs==null?null:(rs+120)/2,'ul');
 const si=l.lte_sinr; document.getElementById('sinr').innerHTML=`<span class="${cls(si,15,5)}">${fmt(si)} dB</span>`; const sb=document.getElementById('sinrb'); sb.style.width=si==null?'0':pct(si,-5,30); sb.style.background=col(si,'sinr');
 document.getElementById('nr').innerHTML=l.nr_band?`${l.nr_band} · ${fmt(l.nr_bw)} MHz · <span class="${cls(l.nr_sinr,15,5)}">SINR ${fmt(l.nr_sinr)}</span>`:'<span class="dim">none</span>';
 document.getElementById('gps').textContent=(l.lat!=null?`GPS (${l.gps_src}) ${l.lat.toFixed(5)}, ${l.lon.toFixed(5)} · ${fmt(l.speed_kmh)} km/h`:'no GPS fix yet')+` · ${st.n} samples`+(st.status&&st.status!=='ok'&&st.status!=='no GPS fix'?' · '+st.status:'');
 for(const s of st.samples){ if(s.lat==null)continue; const v=colorBy==='ul'?(s.ul_probe_mbps??s.ul_passive_mbps):s.lte_sinr;
   L.circleMarker([s.lat,s.lon],{radius:s.ul_probe_mbps!=null?7:4,color:col(v,colorBy),fillColor:col(v,colorBy),fillOpacity:.85,weight:s.ul_probe_mbps!=null?2:0})
    .bindTooltip(`${new Date(s.ts*1000).toLocaleTimeString()} · ${(s.rat||'').replace('NR5G-','5G ')} ${s.ca||''}<br>test ${fmt(s.ul_probe_mbps)} Mbps · SINR ${fmt(s.lte_sinr)}`).addTo(layer); }
 drawn=st.n;
 if(l.lat!=null){ if(!me){me=L.circleMarker([l.lat,l.lon],{radius:9,color:'#fff',weight:3,fillColor:'#39f',fillOpacity:1}).addTo(map); map.setView([l.lat,l.lon],16);} else me.setLatLng([l.lat,l.lon]); if(follow)map.panTo([l.lat,l.lon]); }
 const ct=document.querySelector('#ct tbody'); ct.innerHTML=''; const serving=l.lte_pci;
 const nb=[...(l.neighbours||[])]; if(l.lte_band)nb.unshift({band:l.lte_band,pci:l.lte_pci,rsrp:l.lte_rsrp,rsrq:l.lte_rsrq,serving:true});
 nb.sort((a,b)=>(b.rsrp??-999)-(a.rsrp??-999)).forEach(n=>{const tr=document.createElement('tr');tr.innerHTML=`<td style="text-align:left">${n.band}${n.serving?' <span class="dim">●serving</span>':''}</td><td>${fmt(n.pci)}</td><td class="${cls(n.rsrp,-80,-95)}">${fmt(n.rsrp)}</td><td>${fmt(n.rsrq)}</td>`;ct.appendChild(tr);});
 const bt2=st.bandtest||{}; document.getElementById('bandtest').disabled=!!bt2.running; document.getElementById('btlog').textContent=(bt2.log||[]).slice(-4).join('\n');
 const btt=document.querySelector('#btt tbody'); btt.innerHTML=''; (bt2.results||[]).forEach(r=>{const tr=document.createElement('tr');tr.innerHTML=`<td style="text-align:left">${r.name}</td><td class="${cls(r.mbps,80,40)}"><b>${fmt(r.mbps)}</b> Mbps</td><td class="dim">${r.note||(r.ca||'')}</td>`;btt.appendChild(tr);});
 const sc=st.scan||{}; document.getElementById('scan').disabled=!!sc.running; const sct=document.querySelector('#sct tbody'); sct.innerHTML='';
 if(sc.cells&&sc.cells.length){const h=document.createElement('tr');h.innerHTML='<th style="text-align:left">full scan</th><th>PLMN</th><th>PCI</th><th>RSRP</th>';sct.appendChild(h);
   sc.cells.slice(0,25).forEach(c=>{const tr=document.createElement('tr');tr.innerHTML=`<td style="text-align:left">${c.rat} ${c.band}</td><td class="dim">${c.plmn}</td><td>${fmt(c.pci)}</td><td class="${cls(c.rsrp,-80,-95)}">${fmt(c.rsrp)}</td>`;sct.appendChild(tr);});}
 mastLayer.clearLayers(); (st.masts||[]).forEach(m=>{ L.circle([m.lat,m.lon],{radius:Math.max(40,m.spread_m),color:'#c084fc',weight:1,fillOpacity:.08}).addTo(mastLayer);
   L.marker([m.lat,m.lon],{icon:L.divIcon({className:'',html:`<div style="background:#c084fc;color:#000;border-radius:6px;padding:1px 5px;font-size:11px;font-weight:700;white-space:nowrap">📡 ${m.id}</div>`})}).bindTooltip(`estimated mast ${m.id}<br>${m.n} readings · best RSRP ${m.best_rsrp} · ±${m.spread_m} m`).addTo(mastLayer); });
 bestLayer.clearLayers(); const tb=document.querySelector('#bt tbody'); tb.innerHTML=''; document.getElementById('besthint').style.display=st.best.length?'none':'';
 st.best.forEach((b,i)=>{ if(i<10)L.marker([b.lat,b.lon],{icon:L.divIcon({className:'',html:`<div style="background:#ffd166;color:#000;border-radius:10px;padding:1px 6px;font-weight:700">${i+1}</div>`})}).addTo(bestLayer);
   const q=pcc(b.ca); const tr=document.createElement('tr'); tr.className='spot'; tr.innerHTML=`<td>${i+1}</td><td class="${cls(b.ul_mbps,80,40)}"><b>${fmt(b.ul_mbps)}</b></td><td>${fmt(b.sinr)}</td><td style="text-align:left">${(b.rat||'').replace('NR5G-','5G ')} ${q?q.band+' '+q.bw+'MHz':''}</td><td class="dim">${new Date(b.ts*1000).toLocaleTimeString().slice(0,5)}</td>`;
   tr.onclick=()=>{follow=false;document.getElementById('follow').classList.remove('on');map.setView([b.lat,b.lon],17)}; tb.appendChild(tr); });
}
document.getElementById('follow').onclick=e=>{follow=!follow;e.target.classList.toggle('on',follow)};
document.getElementById('probe').onclick=()=>fetch('/api/probe',{method:'POST'});
document.getElementById('bandtest').onclick=()=>{if(confirm('Locks the modem to each band in turn and runs a 4 MB test on each. Stay parked; the Mudi link drops for ~30 s per band. Start?'))fetch('/api/bandtest',{method:'POST'});};
document.getElementById('scan').onclick=()=>{if(confirm('Full band sweep: the Mudi link drops for 1-2 minutes. Start?'))fetch('/api/scan',{method:'POST'});};
document.getElementById('center').onclick=()=>{follow=true;document.getElementById('follow').classList.add('on');if(me)map.setView(me.getLatLng(),17);};
document.getElementById('color').onclick=e=>{colorBy=colorBy==='ul'?'sinr':'ul';e.target.textContent='color: '+(colorBy==='ul'?'upload':'SINR');layer.clearLayers();drawn=0;};
document.getElementById('geo').onclick=e=>{ if(geoWatch!=null){navigator.geolocation.clearWatch(geoWatch);geoWatch=null;e.target.classList.remove('on');return;}
  geoWatch=navigator.geolocation.watchPosition(p=>{window.lastPos={lat:p.coords.latitude,lon:p.coords.longitude,speed_kmh:p.coords.speed==null?null:p.coords.speed*3.6};fetch('/api/pos',{method:'POST',body:JSON.stringify(window.lastPos)});},null,{enableHighAccuracy:true,maximumAge:1000}); e.target.classList.add('on'); };
setInterval(()=>{ if(window.lastPos) fetch('/api/pos',{method:'POST',body:JSON.stringify(window.lastPos)}); },4000);
setInterval(tick,2000); tick(); if(navigator.geolocation) document.getElementById('geo').click();
</script></body></html>
"""


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _json(self, obj):
        b = json.dumps(obj).encode()
        self.send_response(200); self.send_header("Content-Type", "application/json"); self.send_header("Content-Length", str(len(b))); self.end_headers(); self.wfile.write(b)

    def do_GET(self):
        if self.path.startswith("/api/state"):
            m = re.search(r"since=(\d+)", self.path); since = int(m.group(1)) if m else 0
            with lock:
                self._json({"latest": state["latest"], "samples": state["samples"][since:], "n": len(state["samples"]),
                            "best": state["best"], "masts": state["masts"], "status": state["status"], "probe_busy": state["probe_busy"],
                            "bandtest": state["bandtest"], "scan": {"running": state["scan"]["running"], "cells": state["scan"]["cells"]}})
        else:
            b = HTML.encode()
            self.send_response(200); self.send_header("Content-Type", "text/html; charset=utf-8"); self.send_header("Content-Length", str(len(b))); self.end_headers(); self.wfile.write(b)

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0); body = self.rfile.read(n) if n else b""
        if self.path == "/api/pos":
            try:
                p = json.loads(body); state["browser_pos"] = {"lat": float(p["lat"]), "lon": float(p["lon"]), "speed_kmh": p.get("speed_kmh"), "t": time.time()}
            except Exception:
                pass
        elif self.path == "/api/probe":
            state["probe_now"] = True
        elif self.path == "/api/bandtest" and not state["bandtest"]["running"] and not state["scan"]["running"]:
            threading.Thread(target=bandtest_thread, daemon=True).start()
        elif self.path == "/api/scan" and not state["scan"]["running"] and not state["bandtest"]["running"]:
            threading.Thread(target=scan_thread, daemon=True).start()
        self._json({"ok": True})


def main():
    global args
    ap = argparse.ArgumentParser()
    ap.add_argument("--iface", default="en12", help="interface that leads to the Mudi (en12 = USB, en0 if on its Wi-Fi)")
    ap.add_argument("--probe-every", type=float, default=20)
    ap.add_argument("--probe-mb", type=int, default=8)
    ap.add_argument("--no-probe", action="store_true", help="passive only (e.g. while the big uploader runs)")
    ap.add_argument("--port", type=int, default=8765)
    args = ap.parse_args()
    threading.Thread(target=poller, daemon=True).start()
    srv = ThreadingHTTPServer(("0.0.0.0", args.port), H)
    print(f"map: http://localhost:{args.port}   csv: {CSV}   iface: {args.iface}   probe: {'off' if args.no_probe else f'{args.probe_mb} MB every {args.probe_every:g} s'}")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
