# Timing evidence and diagnostic adapter

## Normalized recorder input

POST JSON to `/api/timing` after the referenced full cell and a suitable GPS fix have been observed:

```json
{
  "ts": 1789700000.125,
  "key": "nr:262-01:1234001",
  "reference_key": "nr:262-01:1234001",
  "encoding": "nr-absolute-rar",
  "ta_index": 12,
  "scs_khz": 30,
  "source": "Validated diagnostic feeder",
  "timing_group": "verified group",
  "range_error_m": 50
}
```

Replace the illustrative timestamp with the actual current measurement time in Unix seconds. Keys use lower-case `lte`/`nr`, PLMN, and normalized upper-case hexadecimal ECI/NCI. The timing reference must match the cell that establishes the propagation range; membership in a shared timing group is not enough to locate every carrier's emitter.

The recorder requires an observed full identity, a matching radio observation within two seconds, GPS within 1.5 seconds and accuracy ≤25 m. Timestamps must be no more than 30 seconds old or two seconds ahead. Duplicate or out-of-order timing timestamps per cell are not appended again. These checks validate an ingestion contract, not the truth of an external feeder's attribution.

| Encoding | Index range | Range step |
|---|---|---|
| `lte-absolute-16ts` | Integer 0–1282 | Approximately 78.071 m |
| `nr-absolute-rar` | Integer 0–3846 | Approximately 78.071 × 15 / uplink SCS(kHz) m |

For NR, `scs_khz` must explicitly provide actual uplink spacing (15/30/60/120/240), not the modem's numeric enum. Fixed timing offsets, chipset ticks and incremental commands must be converted through a separately validated adapter. Raw incremental commands must not be posted as absolute range indices.

Uncertainty uses a full quantization step, reported GPS accuracy, and `range_error_m` (default 50 m). Reflections and unknown offsets can exceed this allowance. It is not a statistical confidence interval. Estimates require at least four attributed observations with useful geometry. Site associations are candidates only.

## LTE adapter inherited from the older tool

`diagnostic_timing.py` reuses `parse_b062_rach` from the original tool behind extra bounds/version checks. Its B063 parser is adapted from the older implementation while retaining the upper two TAG bits. It supports the legacy B062 subpacket versions 2/0x32 and B063 body versions 0x31/0x32 used by that code. Supported layout numbers alone do not establish firmware correctness.

A capture feeder must supply JSONL records with:

- `code`: `B062` for absolute RACH TA or `B063` for MAC TA commands.
- `body_hex`: the deframed log body as hexadecimal. The capture feeder must check framing/CRC before supplying it.
- `key` and identical `reference_key`: the full LTE cell that actually establishes this timing reference.
- `epoch`: a new nonempty attachment/handover generation identifier whenever timing context may change, including returning to the same cell.
- `tag_id`: the verified timing group, integer 0–3.
- `sequence`: consecutive nonnegative integers for this stream. Gaps or duplicates invalidate accumulated TA until a new absolute reading.
- `ts`: actual measurement time. Backward timestamps or gaps over two seconds also invalidate accumulated TA.

The accumulator never invents absolute TA from deltas. Commands for another TAG are ignored. Invalid or out-of-range data clears the current absolute value. The supplied epoch and identity remain the feeder's responsibility; polling the current serving PCI is insufficient.

Decode attributed captures without contacting a recorder:

```sh
python3 diagnostic_timing.py --input attributed-capture.jsonl > normalized-timing.jsonl
```

A qualified live producer can stream JSONL through stdin:

```sh
python3 diagnostic_timing.py --url http://127.0.0.1:8787
```

Without `--url`, normalized events are printed only. With it, events are POSTed to `/api/timing`; the recorder's freshness and GPS checks still apply. Historical captures are not made live by rewriting timestamps, and old captures will be rejected by the live API. The command never opens SSH, starts diagnostics, sends keepalive traffic, changes bands or reconnects a modem.

**Not yet implemented:** a verified RG650V capture/identity bridge feeding this adapter, or a live NR diagnostic decoder. The old standalone `ta_locate.py --diag` remains a separate experimental tool with disruptive setup and weaker grouping; it is not invoked here.

## Review and replay

Select a full identity and open **Timing evidence**. Show/hide rings, fit the map to their extent, scrub or replay the latest sixty retained rings, or return to **Latest**. A historical fit uses only rings at or before the selected time. Its candidate-site matches are recomputed for that evidence. The convergence trail records recent tentative fits during the current recorder run; it is not a confidence claim and is not persisted as a surveyed trajectory.

The main radio map and GPS continue live while timing history is reviewed. The evidence panel identifies the selected review time. Full observations remain in SQLite/CSV beyond the in-memory ring window.

## Primary references

- [ETSI LTE physical-layer procedures](https://www.etsi.org/deliver/etsi_ts/136200_136299/136213/17.05.00_60/ts_136213v170500p.pdf): absolute and incremental timing advance differ.
- [ETSI NR physical-layer procedures](https://www.etsi.org/deliver/etsi_ts/138200_138299/138213/16.04.00_60/ts_138213v160400p.pdf): timing advance and uplink numerology.
- [Bundesnetzagentur EMF information](https://www.bundesnetzagentur.de/DE/Vportal/TK/Funktechnik/EMF/start.html): public site coordinates can be displaced by up to 80 m.
