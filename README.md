# Mudi drive survey

Find the spot with the fastest cellular **upload** using a GL.iNet Mudi 7 (GL-E5800, Quectel RG650V) and a laptop, while driving.
No phone or extra hardware needed: the modem's own GNSS provides the position.

![panel](docs/panel.png)

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
