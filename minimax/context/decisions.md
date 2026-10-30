# Active decisions

- The OceanBase/seekdb tables are the authoritative task ledger; evidence files
  are review exports only.
- MiniMax may change only paths named by `allowed_changes`; specification and
  regression tests are immutable during a run.
- A failed verification restores every changed file and records a failed run.
- Reuse requires the same task definition, retrieved context, and current code
  digest as a previously verified result.
