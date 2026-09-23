# Changelog

## Unreleased

- PR 0: audited existing assets; introduced read-only Zotero discovery with pagination consistency checks and private export, synthetic tests, public CI, development rules, candidate screening and staged evaluation specification.
- No target simulation, model runtime or scientific reproduction is delivered yet.

- PR 1a: durable accounting and dispatch coordination with cross-process budget tests; no production scheduler adapter yet.
- Zotero schema v2 distinguishes reference records from DOI groups and preserves duplicate evidence.
- PR 1b: content-addressed private input snapshots and bounded, durably audited Slurm read-only lookup; no production submit, cancellation or ledger reconciliation writeback yet. MIT state-map attribution retained.
- PR 1c: atomic scheduler reconciliation, durable query ordering, recovery of terminal but unaccounted jobs, campaign-wide conflict holds, and administrator one-pass recovery command. Validated with synthetic receipts; actual job recovery remains unverified.
