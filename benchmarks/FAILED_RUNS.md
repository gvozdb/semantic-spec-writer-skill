# Failed preregistered runs

Failed runs stay visible. They are not retried, filtered, or promoted as
benchmark evidence.

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
