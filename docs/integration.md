# Integration provenance and limits

Repository: `michaelschleiss/mudi-drive-survey`.
Branch: `feature/cell-hunt-integration`.

This is the single maintained app for this task. The separate `uplink-atlas` repository has been archived and its local checkout retired. There is no runtime dependency on that checkout. The original local `mudi-drive-survey` working directory was preserved; integration happened in an isolated Git worktree.

## Sources combined

| Source | Retained or adapted |
|---|---|
| Original GitHub main, `e792fc0` | Modem parsing, radio queries, original standalone survey and TA tools; experimental LTE diagnostic decoding |
| Newest local app snapshot, committed as `30a9f96` | Cell-hunting workspace, neighbours/aggregation panels, band and signal filters, linked charts, phone view, warmed parked verification, SQLite session schema |
| Uplink Atlas, `163e63c` | Asynchronous journal, strict identity validation, conservative GPS matching, absolute TA validation, geometry checks, site candidate import and matching |
| Integration work | Missing identities stay unresolved; timing-history replay and convergence trail; guarded LTE accumulator retains timing-group bits; restart/offline recovery and expanded tests |

Full identities and coincident radio evidence govern the inventory. All bands remain eligible; a band name does not prove upload performance. Raw live SINR is not treated as calibrated dB. The original scripts remain separate research tools and are not implicitly started by the integrated app.

## Tested behavior

- Full IDs normalize hexadecimal spelling and distinguish cells sharing PCI.
- GPS and snapshots continue while disk writes or optional upload attempts are blocked.
- Existing local-app SQLite recordings resume, with verification disarmed.
- Timing input requires matching full identity, encoding, timestamps and suitable GPS.
- Synthetic localization rejects weak geometry; candidate matching respects known operator mismatches.
- LTE diagnostic fixtures retain TAG bits and reset accumulated timing on gaps, identity/epoch changes or malformed input.
- Browser flows cover passive collection, radio-map layers, linked charts, timing replay, candidate fit, export, desktop/phone/mobile-map layout, recorder restart and offline state.

## Hardware boundary

The AT reader is implemented, but live operation still needs router SSH access and a hardware trial. The previously attempted router check failed host-key verification; it was not bypassed.

The diagnostic adapter consumes attributed records. It does not establish that a particular RG650V packet layout, identity or timing group was decoded correctly on real hardware. A verified live capture/attribution bridge is still needed. No live NR timing decoder exists. Synthetic demo rings are not evidence of live capture.

The old `ta_locate.py --diag` performs diagnostic-router reconfiguration and forced re-registration and identifies its live observations by band/PCI. Its historical README statement of a stationary successful read does not validate the new attribution contract. Do not connect it to the strict recorder by merely renaming a PCI as a full identity.

No BNetzA data is bundled. GPS accuracy, polling cadence, range-model accuracy and real upload performance remain field-validation tasks.
