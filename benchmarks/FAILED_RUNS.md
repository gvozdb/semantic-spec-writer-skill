# Failed preregistered runs

Failed runs stay visible. They are not retried, filtered, or promoted as
benchmark evidence.

## 2026-09-03: Markdown vs Capsule lifecycle v2

- Code revision: `3bc5817cd7f48ac6ae6f19e1fef73f9adf3c6777`
- Run: `20260903T102315Z`
- Protocol: `markdown-capsule-full-lifecycle-dominance-v2`
- Model: `gpt-5.6-terra`, medium reasoning
- Corpus: `refund-ledger`, `tenant-settings`, `webhook-dispatch`
- Authoring: 3/3 valid handoffs, 5 charged model calls, 1,454,228 tokens
- Implementation: 6/9 successful for Markdown and 6/9 for Capsule
- Capsule routed action gate: 6/9
- Implementation tokens: 1,222,545 Markdown; 631,763 Capsule (-48.32%)
- Measured three-use lifecycle: 1,222,545 Markdown; 2,085,991 Capsule
  (+70.63%)
- Average one-use lifecycle per task: 135,838 Markdown; 554,939 Capsule
  (+308.53%)
- One-use pair wins: 0/9; fixture-cluster 95% CI
  `[-358.964%, -277.637%]`
- Outcome: failed quality, routed-action, one-use token, confidence-interval,
  and measured three-use token gates
- Publication: none; no token-saving or quality claim was made
- Retry: none, as required by the preregistration

The skill cut implementation-stage tokens by 48.32%, but two fixtures required
their conditional second authoring attempt. The complete authoring bill erased
the implementation saving at the measured reuse count. The same measured
implementation rates imply an eight-use break-even, but that extrapolation is
not claim-eligible because the quality and action gates also failed.

The privacy-redacted [result](results/failed/gpt-5.6-terra-medium-20260903-markdown-capsule-lifecycle-v2/capsule-r3.json)
and [report](results/failed/gpt-5.6-terra-medium-20260903-markdown-capsule-lifecycle-v2/CAPSULE.md)
are retained under `results/failed`, never `results/published`. Their SHA-256
digests are:

- result: `352fca51066dab49f31c48fc05d2e72c679c30a6ba85b77e17d773d28beed10c`
- report: `5bf2349ef9c409ae801f91b7d5e434fec73816f52943105b9f72bd0714542bbb`

## 2026-09-03: Markdown vs Capsule lifecycle v1

- Code revision: `ace4bc3a839b150ca49513896f113ecfca3767cb`
- Protocol: `markdown-capsule-full-lifecycle-dominance-v1`
- Model: `gpt-5.6-terra`, medium reasoning
- Corpus: `refund-ledger`, `tenant-settings`, `webhook-dispatch`
- Planned work: one authoring attempt per fixture, then three paired
  implementation repetitions per arm
- Outcome: failed during authoring before any implementation run
- Failure: `tenant-settings` did not produce a valid handoff on its only
  permitted attempt
- Publication: none; no token-saving or quality claim was made
- Retry: none, as required by the preregistration

The v1 harness kept its intermediate authoring checkpoint in a temporary
directory and removed it when the authoring gate raised. The terminal failure
was retained, but no complete per-attempt JSON survived, so this run cannot
support partial token or quality statistics. The harness was subsequently
changed to persist a privacy-redacted authoring-failure record before raising.

Lifecycle v2 is a separately preregistered protocol, not a rerun of v1. It
permits one conditional in-run repair after deterministic authoring validation
fails and charges every attempt's input and output tokens. The quality and token
release thresholds remain unchanged.
