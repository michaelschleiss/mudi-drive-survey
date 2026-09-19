# Mudi drive survey

## Upload Atlas — continuous drive survey

The new dashboard runs with **Python 3.9+**, macOS `curl`, and the existing
Mudi SSH connection. It keeps GPS, modem readings, interface traffic, and upload
tests independent. The original `mudi_survey.py` remains available as the legacy tool.

Preview without connecting to hardware or uploading test data:

```sh
python3 drive_app.py --demo
```

Open `http://localhost:8765`, then press **Start upload tests** to see simulated
measurements. The simulation is explicitly labelled and never saved to the real database.

### Cell hunting is the default

GPS and radio record automatically; upload tests are **off**. The Mac map starts in **RSRP**; the optional
**All-band hunt** layer uses these clues: every reported serving band is considered. Green means reported
channel width ≥20 MHz, RSRP ≥−105 dBm and SINR ≥10 dB together; amber meets the
width or signal criterion; grey has other or incomplete evidence. These thresholds
are a scouting heuristic, never a predicted Mbps rate. Band filters are available
on the map and in the discovery inventory. The phone shows the serving band, reported channel width and signal.
The map plots receiver GPS positions, not inferred mast coordinates. n78 is a lead
for further investigation, not proof of fast uploads or an exhaustive scan of all bands.

**Places to revisit** preserves discovered serving cells across restarts and prioritizes
located observations meeting the width and signal criteria, then SINR and width.
No band gets an automatic advantage. Candidate evidence comes from one observation;
separate historical signal peaks are not combined. Each entry shows
operator, cell ID (when available), channel, PCI, counts, strongest signal and candidate GPS observation. The candidate also retains
its reported carrier combination and LTE anchor. Those are not proof of uplink
aggregation; displayed channel width is not necessarily uplink width.
Click it to return to that observed location. GPS joins require a fix within 3 seconds
and accuracy of 30 m or better; there is no claim of exact mast localization.
LTE and NR SA retain their reported cell IDs. NR NSA reports a partial radio signature
(operator/band/channel/PCI), which can repeat at different sites. Missing historical
identity fields cannot be recovered. Neighbour cells remain visible in the radio panel;
the discovery inventory currently covers serving cells, not all nearby transmitters.

The Quectel [QENG documentation](https://www.quectel.com/content/uploads/2024/05/Quectel_RG50xQRM5xxQ_Series_AT_Commands_Manual_V1.2.pdf)
describes the fields exposed by the modem. These reports do not guarantee visibility
of every nearby NR cell. No disruptive band lock, scan or re-registration is enabled.

**Verify parked spot** explicitly starts three longer upload attempts and then stops.
The older driving-upload API remains available but the normal interface offers parked
verification only. Upload results and old rankings live in the expandable history.

### Signal measurements on the GPS track

The default map layer is **RSRP**. Choose RSRP, SINR, RSRQ, stability, reported
channel width, bands, scouting clues or recorded upload footprints. The same controls
are available on the dashboard and full-screen map. A cell selector follows one
identity; the remaining GPS route stays faint. Click a coloured observation for
its measurements, identity, timestamps, GPS accuracy and radio/GPS time offset.

Radio observations use the latest accurate GPS fix within 3 seconds, without
extrapolating a mast location. Gaps longer than 3 seconds and fixes worse than
30 m are not connected as measured track segments. Grey metric points mean unknown.
Orange rings mark an **observed serving-cell identity change**, not a change in
aggregation alone. Partial NSA identities can repeat at different sites.

The selected-cell inspector links RSRP, SINR and RSRQ charts to map points.
Click a chart to select its receiver location. The horizontal axis can show time
or travelled distance across available GPS-matched segments; GPS gaps contribute
no invented distance. The chart never joins separate cell visits or missing readings.
Stability is rolling RSRP standard deviation over up to 10 seconds of contiguous
same-cell observations, requiring at least five readings. It describes variation,
not strong coverage, repeat-visit reliability or measured uplink capacity.

The interactive track retains the latest 12,000 GPS-matched radio observations;
raw SQLite/CSV history remains complete. Dense maps simplify points for rendering,
while charts retain the loaded readings. Timing advance remains unavailable pending
validation. No distance rings or estimated mast pins are fabricated.

### Cell-hunting workspace layout

On desktop, opening the app now goes directly to the map workspace. The left sidebar
keeps recorded cells visible. Select a cell there or via **Follow cell** above the map.
The map has its own unobstructed area; selected-cell readings and linked charts open
in a bottom panel. Signal layer and band filters are always visible above the map.
There is no oversized live-band card covering the route.

**Live modem details** and **Parked upload verification** are expandable secondary
sections at the bottom of the sidebar. **Review session** opens the full dashboard;
**Phone GPS** switches to the phone display, whose **Map view** button returns.
The layout adapts to laptop heights; the phone keeps its simpler foreground GPS view.

### Two live displays

- **Mac:** choose **Live map**, or open `/?view=map`. The map fills the window,
  following the phone's position with upload speed, recording/GPS status and
  best spots overlaid. **Full screen** hides browser chrome where supported;
  **Details** reveals telemetry and ranked spots. Dragging the map stops follow;
  **Follow position** returns to the current location.
- The Mac map also keeps a **Live radio** panel visible: LTE/5G bands, PCI,
  reported bandwidth, RSRP/SINR, LTE RSRQ and neighbouring cells. **All radio
  details** opens the full table and recent band/cell-change timeline. **Fit
  recorded route** shows the recorded path. The map layer selector offers
  upload tests, radio SINR and serving bands; radio points use GPS fixes matched
  to radio readings within 3 seconds. Without located upload tests, the map
  initially shows the available serving-band observations.
- **Recorded uploads** preserves successful and failed tests even without a
  GPS match. The last completed upload remains visible with its measurement age;
  it is not presented as current interface traffic.
- **Phone:** open `/?view=phone` at the Mac's trusted HTTPS address. The drive
  display has a large upload reading, best recorded result, GPS/recording status,
  GPS enable button and upload pause/resume. Supported browsers are asked to
  keep the screen awake; keep the page in the foreground.
- Both pages share one backend and one recording. Pausing uploads from either
  page updates both displays while GPS/radio recording continues. **Exit view**
  returns to the full review dashboard.

The simulation binds only to localhost when launched with `--host 127.0.0.1`;
phone use still needs LAN access and the trusted HTTPS setup described below.

For a real drive with the Mudi connected by USB:

```sh
python3 drive_app.py --iface en12
```

Use your actual Mudi interface (`--iface en0` when appropriate for Wi-Fi).
The application starts recording immediately; upload traffic starts only when
you press **Start upload tests**. Pausing finishes any current test, bounded by
a 25-second driving / 55-second parked batch deadline, and leaves GPS/radio collection running.

### Acquisition and measurement design

| Stream | Default target | Behavior |
| --- | --- | --- |
| GPS | Every source callback | Saves original timestamp, accuracy and speed; repeated fixes are rejected |
| Serving cell | 1 second | One serialized modem worker; no overlapping AT commands |
| Carrier aggregation / neighbours | Every fifth radio poll | Keeps slower commands out of most polling cycles |
| Interface upload traffic | 0.5 seconds | Includes all outgoing traffic; never used to rank capacity |
| Driving upload | ~5 seconds + 1-second pause | At least two sequential accepted chunks; adaptive 256 KiB–32 MiB each |
| Parked verification | Three ~20-second batches | At least eight sequential chunks per batch; stops automatically after three attempts |
| Dashboard | 0.5 seconds after each response | Incremental updates with monotonic cursors and reconnect recovery |

These are scheduling targets, not guaranteed hardware rates. Slow SSH/modem
responses do not stop GPS or uploads. The dashboard shows actual modem latency
and data age. GPS frequency is controlled by the source; polling an old fix
more often does not create new position information.

Uploads use the [Cloudflare speed-test POST endpoint](https://github.com/cloudflare/speedtest)
`https://speed.cloudflare.com/__up`. Only HTTP 2xx responses with the complete
payload qualify as completed tests. The default interface offers **Parked · 3 × ~20 s** only;
passive collection during driving requires no button press. Pause and let the current test finish before changing mode.
Parked verification stops after three attempts, including failures. A parked test
moving more than 20 m is not accepted for ranking.

Each batch sends a 256 KiB warm-up then uses the same curl process/connection pool
for its measured chunks. Fast links use more chunks to avoid truncating the duration
at the 32 MiB per-request payload cap. Connection reuse is recorded (the server may close a connection).
The average is measured payload bytes divided by summed measured request durations;
initial connection setup and warm-up are reported separately. Request/response waits
and any reconnections during the measured batch remain included. Chunk minimum and
maximum show variability, not instantaneous radio capacity. Live progress reports
completed chunks only; interface traffic remains a separate counter.

The payload adapts to target duration using the previous measurement. These are
**duration targets, not fixed timers**: actual duration is always shown. Batches below
80% of the target remain in history but cannot rank a spot or set a new best record.
Payload caps and rapid speed changes can prevent the duration target being reached.
GPS paths cover the measured batch; a moving result describes a road segment.
Located upload failures contribute zero to the lower-quartile spot score; each spot
shows attempts, failures, and qualifying parked measurements. Historical failures
without a recorded location qualification remain unranked. Old successful results
are retained and labelled with their earlier measurement method in history.

Rejected or interrupted requests remain in the log; failures back off up to
60 seconds. There are **no download speed tests**. Small protocol responses
and optional OpenStreetMap tiles still download; **Basemap off** stops new tile requests.
These tests measure goodput to Cloudflare, not maximum modem capacity or speed to
every destination. Use parked verification to compare promising moving results.

For your own accepting endpoint, including presigned object uploads:

```sh
export ATLAS_UPLOAD_URL='https://your-upload-endpoint'
python3 drive_app.py --iface en12 --upload-method PUT
```

The endpoint must consume the entire body and return a small 2xx response.
Upload tests can consume substantial data: a continuous 100 Mbps payload stream would send about **45 GB/hour**.
Completed-batch data totals include warm-up traffic. Increase `--probe-every`
(the pause after each batch, default 1 second) to reduce usage. Configure your actual
destination for final comparisons; a public test endpoint can change or throttle.

### GPS for driving

**Use phone GPS as the primary source.** Mac Location Services estimates
location from nearby Wi-Fi networks, so it is a fallback rather than a reliable
source for road-scale spot ranking. See [Apple's location explanation](https://support.apple.com/en-sa/guide/mac-help/mh27621/mac).

The browser uses `watchPosition` with high accuracy and no cached fixes.
On a phone, serve the dashboard over **trusted HTTPS**: browsers do not permit
geolocation on a plain `http://<laptop-ip>` LAN page. A trusted certificate must
match the hostname/IP used by the phone. You can terminate HTTPS in a local
reverse proxy or provide a certificate and key directly:

```sh
python3 drive_app.py --iface en12 --tls-cert /path/to/cert.pem --tls-key /path/to/key.pem
```

Connect the phone to the Mudi Wi-Fi, open the HTTPS page on the laptop, and tap
**Use this device’s location**. Keep the page in the foreground and the phone
awake; mobile browsers can suspend background location updates. The app marks
stale fixes and excludes them from ranking instead of silently reusing them.

For the Mac fallback, use the same button on localhost, or run:

```sh
swift laptop_location.swift 8765
```

The Swift feeder sends new Core Location callbacks with their original timestamps
and reported accuracy. It no longer re-posts an old fix on a timer. Native GPS
feeders can POST `{lat, lon, ts, acc_m, speed_kmh, source}` to `/api/pos`; `ts`
is Unix time in seconds. Device clocks must agree. The Mudi's unconnected GNSS
antenna is not polled by the new collector.

### Trustworthy spot ranking and persistence

Every upload retains its start/end time and GPS footprint. To rank, fixes must
cover the test with no gaps greater than 3 seconds, reported accuracy ≤30 m,
and a footprint no wider than 80 m from its initial fix. The marker matches the
test's temporal midpoint, using a GPS fix at that timestamp or linear
interpolation between bracketing fixes. No stale-position extrapolation is used.
The recorded GPS path during each test is drawn in its upload-speed colour;
speed describes the test interval, not an instantaneous reading at the pin.
Raw fix timestamps and accuracy remain in the export. Nearby tests within 40 m of
a spot's anchor are grouped. Spots are ordered by their lower-quartile result;
median, count and provisional/repeated status show how much evidence exists.
Three tests mean repeated evidence, not statistical certainty. Passive traffic,
signal-only readings, failed tests and poorly located tests never enter rankings.

All observations are committed to SQLite (WAL) as they arrive. `--db` chooses
the file; reuse it to resume a survey, or use a new filename for a separate
drive/operator/session. The last 12,000 events are retained in the live feed;
the database and **Export survey** retain the complete record. The map bounds
its rendered history for responsiveness. Ranking is incremental, without
rescanning the entire drive on each radio sample.

The new drive dashboard deliberately has no band-lock or full-scan controls,
which interrupt connectivity. Those legacy parked diagnostics are available
in the original app; do not run them concurrently with a drive survey.

Validation: `python3 -m unittest discover -s tests -v`.

---

## Legacy survey tool

Find the spot with the fastest cellular **upload** using a GL.iNet Mudi 7 (GL-E5800, Quectel RG650V) and a laptop, while driving.

**Position source:** a phone. The Mudi 7's modem has a GNSS receiver but GL.iNet confirmed the antenna pin is left
floating on the board (https://forum.gl-inet.com/t/mudi7-gps/68619), so it never sees a satellite. Open the map page
on a phone connected to the Mudi Wi-Fi (`http://<laptop-ip>:8765` / `:8766`), allow location, and both tools use the
phone's GPS. They still poll the modem GNSS and would prefer it if a fix ever appeared.


## What it does

Every 2 s (over `ssh root@192.168.8.1`):

- **Position** from the modem GNSS (`AT+QGPSLOC`), or the browser's geolocation as fallback.
- **Serving cell**: LTE / 5G NSA / 5G SA, bands, bandwidths, PCI, RSRP, RSRQ, SINR.
- **Carrier aggregation** (`AT+QCAINFO`), e.g. `B3(20)+B3(10)+B8(10)+n1(20)`. The first entry is the primary carrier, which is what your upload rides on.
- **Neighbour cells** (`AT+QENG="neighbourcell"`): every LTE cell the modem can hear, with band and strength. A passive scanner that never interrupts the link.

Plus real throughput on the Mudi link:

- **Active probe** every 20 s: an 8 MB PUT to `storage.googleapis.com` bound to the Mudi interface (the server answers 400; only the upload speed matters).
- **Passive rate**: bytes leaving the interface each second.

Parked-only extras (they drop the link for a while):

- **Test bands here**: locks the modem to each band heard (and tries 5G NSA on n78 and 5G SA), re-registers, runs two 4 MB probes, then restores all defaults. ~30-45 s per band.
- **Full scan** (`AT+QSCAN=3`): complete LTE + 5G sweep of all PLMNs with RSRP.

Everything is appended to `~/mnemosyne-recon.csv` and shown live on a Leaflet map at `http://localhost:8765`
(also reachable from a phone on the Mudi Wi-Fi at `http://<laptop-ip>:8765`).

The map shows the track coloured by upload speed (or SINR), the 10 best 60 m cells ranked by their best test,
and **estimated mast positions**: for each band/PCI, the power-weighted centre of everywhere it was heard, with an
uncertainty radius. (Latency-based ranging does not work on cellular: 3.3 µs/km of propagation is buried under
tens of ms of scheduler jitter.)

See [docs/panel-guide.html](docs/panel-guide.html) for a line-by-line explanation of the panel and the colour thresholds.

## Setup

- macOS (uses `netstat -ibn`, `curl --interface`); Python 3 stdlib only.
- Mudi reachable as `root@192.168.8.1` with key-based ssh (`ssh-copy-id root@192.168.8.1` once). The tool batches AT commands through the router's `atcmd`.
- Mudi connected by USB (interface `en12` on the Mac) or by its Wi-Fi (then `--iface en0`).

## Run

```
python3 mudi_survey.py                 # probes on: 8 MB every 20 s (~1.4 GB/h)
python3 mudi_survey.py --no-probe      # passive only, e.g. while another upload is running
python3 mudi_survey.py --iface en0 --probe-every 30 --probe-mb 4
```

Open http://localhost:8765. First GNSS fix takes a minute or two with sky view.

## Reading the numbers

- **Upload carrier** is the line to watch: B3 · 20 MHz or n78 is good; B8/B20 · 10 MHz is a slow anchor.
- **SINR** predicts upload better than RSRP. Strong RSRP with poor SINR means interference between towers.
- Park where the PCI stops changing and three tests in a row agree.

## Status

Written and tested on macOS 27 with a Mudi 7 on Deutsche Telekom (5G NSA, B3+B8+n1). The neighbour-cell scanner,
band test, full scan and mast estimation were added last and have had less road testing than the rest; the AT
syntax was verified on the RG650V, the end-to-end flows not yet. Restores band/mode defaults in a `finally` block
either way.

## ta_locate.py — tower localisation from timing advance

Timing advance is the tower's own range measurement (one LTE step = 78 m one-way). `ta_locate.py` collects
(position, cell, TA) observations along a drive, fits each tower as the point whose distances best match the
range annuli (robust Gauss–Newton, Huber loss, NLOS prior), and shows the process live: every observation as a
fading ring, the fit converging along a dotted trail, and a 2σ uncertainty ellipse. Scrub or replay the drive.

```
python3 ta_locate.py --sim --speed 20          # synthetic drive past three towers, ground truth shown as ✛
python3 ta_locate.py --replay ~/ta-observations.csv --speed 10
python3 ta_locate.py --diag                    # EXPERIMENTAL: TA from the modem's Qualcomm diag port via ssh
```

Open http://localhost:8766. On the simulator the fit lands within 15–65 m of the true masts after a couple of
minutes of driving.

The AT interface of the RG650V does not expose TA, so the live feeder uses the modem's Qualcomm diag stream:
the Mudi's own `diag-router` is restarted for the session with `-s <laptop>:2500` so it streams to the laptop
(the stock daemon is restored on exit). Two log packets are enabled and decoded (layouts from SCAT /
MobileInsight): 0xB062 LTE MAC RACH Attempt gives the absolute TA from the random-access response, and 0xB063
LTE MAC DL Transport Block carries the Timing Advance Command control elements that update it
(`TA += cmd − 31`). A re-registration is forced at start so an absolute TA arrives within seconds; every
handover produces a fresh one. Verified live: stationary, the modem reported TA 11 (≈ 900 m to the serving
mast) and a steady stream of "no change" TA commands. Requires key-based ssh to the Mudi; no extra packages.

### Position from the laptop itself

`swift laptop_location.swift` feeds macOS Location Services (Wi-Fi/cell based, no GPS chip; works in built-up areas
where Apple knows the access points) to both tools every 2 s. macOS must allow Location Services for the terminal app
that runs it: System Settings → Privacy & Security → Location Services → enable your terminal (e.g. Ghostty, Terminal).
A phone on the Mudi Wi-Fi with the page open is more accurate on the road.
