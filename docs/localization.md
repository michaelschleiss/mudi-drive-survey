# Localization evidence and validation

The live map follows the current serving LTE and NR cells. Recorded-cell review
uses the selected cell. A saved hunt target never overrides live ring identity.
LTE and NR are separate transmitters unless independently established otherwise.

## Implemented acquisition

- Radio polls select the router's active SIM explicitly through ubus, recheck SIM
  selection after the batch, and record the subscription. Cell/operator/SIM/state
  changes rotate the association epoch.
- A private QMI NAS client binds only its own control point to SIM1 or SIM2 and
  queries cell location. It does not change network registration or radio modes.
  LTE timing is documented in microseconds. The Vodafone response has been checked
  against an independent SIM2 serving-cell query. Its measurement age is unknown:
  capture timestamps are retrieval timestamps. These snapshots never create rings.
- The continuous diagnostic stream preserves the multi-radio subscription wrapper.
  LTE B062 initial candidates and B063 corrections are recorded separately. B063
  v50 has been tested on real SIM2 packets. B062 has synthetic layout tests only;
  no real initial event has yet been observed on this setup.
- NR B88A v3.18 initial candidates remain proprietary-layout hypotheses. RRC
  parsing covers self-contained SA setup/reconfiguration and NSA secondary groups.
  Partial SA setup without common context is rejected. Subsequent corrections are
  recorded, but not integrated without known carrier/TAG, baseline, sequence and
  alignment validity. `timing_state.py` defines these gates; the current diagnostic
  source does not yet satisfy all of them.

## Mapping rules

Event-time GPS must bracket a diagnostic event within three seconds with reported
accuracy at most 30 m. Radio identity must also bracket the event, agree with the
packet subscription, and remain in the same session/epoch. Ambiguous, old-session,
unknown-subscription or unsupported records remain diagnostic evidence only.

Experimental NR rings include an explicitly assumed allowance: a full timing step,
reported GPS accuracy, and 100 m propagation/model allowance. This is not a measured
error bound. The area estimator uses separated receiver positions and keeps disjoint
solutions. It does not report calibrated confidence or a confirmed mast pin.

## Field validation still required

Use an independently known site coordinate, identified without relying on our own
range estimate. Keep phone GPS connected and record several distinct approaches,
including another direction and a repeat visit. Record the start/end times of each
approach. Do not force modem re-registration merely to manufacture a timing event.

Preserve the survey SQLite database or `/api/export`. Timing snapshots and original
packet payloads are retained so decoding can be rechecked. An unknown-age QMI
snapshot must not be relabelled as a fresh distance when preparing validation data.

`localization_validation.py` evaluates located range rows with `approach_id`, lat,
lon, range_m, acc_m and step_m/quantization_m. It leaves whole approaches out and
reports known-site inclusion, candidate representative error and held-out residuals.
Run `python localization_validation.py --help` for input/output arguments. Synthetic
fixtures check software behavior; they do not establish real-world accuracy.

## Outstanding gates

1. Observe and validate a real LTE B062 initial event, including Msg2 result and
   cell association, or establish a defensible QMI measurement-age bound.
2. Establish complete NR SA context and timing continuity on this firmware before
   accumulating relative corrections. Unknown sequence/expiry cannot be fabricated.
3. Evaluate multiple real approaches and repeat visits against independent site
   coordinates. Fit/check the propagation allowance before describing any area as
   calibrated or showing a confirmed mast position.

## Sources

- QMI NAS schema: https://github.com/linux-mobile-broadband/libqmi/blob/main/data/qmi-service-nas.json
- QMI timing units: https://github.com/linux-mobile-broadband/libqmi/blob/main/src/qmicli/qmicli-nas.c
- Diagnostic layouts: https://github.com/fgsect/scat/tree/master/src/scat/parsers/qualcomm
- NR RRC ASN.1: 3GPP TS 38.331 (decoded through pycrate).
- LTE/NR timing procedures: 3GPP TS 36.213, 36.321, 38.213 and 38.321.
