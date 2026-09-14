# Campaign evidence

Committed, point-in-time copies of the connector campaign's own evidence-catalog
artifacts, mirrored here so a `source.snapshot_ref` citing one of them resolves
for a reader who has only this repository checked out — see
[connector-capability-manifest.md](../../modules/connector-capability-manifest.md)'s
"Resolved this round (W00-S2)" paragraph on `source.snapshot_ref`'s resolution
contract for the rule this directory exists to satisfy.

## What is here

- [`catalog-evidence.json`](catalog-evidence.json) — the campaign's per-service,
  per-operation evidence catalog: which operations are `required`, their
  `category`, and the evidence tier (`source_verified_strict` /
  `search_snippet_or_partial` / `unverified`) backing each claim. Authoritative
  source for the 273-operation reconciliation
  (`services[].operations[]` (232) + `services[].gaps[].demoted_operations_full_record[]`
  (40) + the one contract-attachment operation (1) = 273).
- [`contract-and-dag.md`](contract-and-dag.md) — the campaign's cross-cutting
  contract families (`AUTH`/`GOV`/`RUN`/`KB`/`ACL`/`DATA`/`UX`/`SURF`/`OPS`, 72
  `contract_id` total) and the work-stream DAG this repo's
  [connector-capability-manifest.md](../../modules/connector-capability-manifest.md)
  numbering section restates authoritatively.

`code-audit.json` (the campaign's audit of the existing connector-relevant
surface) is deliberately NOT mirrored here: this PR's own validator, tests,
and spec do not consume it, and `contract-and-dag.md`'s own internal citations
of it point at the campaign's private working file, not an in-repo path — a
future round that actually needs those citations resolvable mirrors the file
here in that round's own commit, per this document tree's owning-spec rule
below. Mirroring a file only a sibling document's PROSE cites, with no
validator or manifest-entry `snapshot_ref` consuming it, is exactly the kind
of forward-looking "we'll want this later" this directory's own resolvability
rule is not meant to justify.

## What this is not

A live sync target. Each file is a **copy as of the commit that added or last
refreshed it** — check this directory's own git history for when the campaign's
shared understanding of its evidence changed, not the campaign's own private
working directory, which keeps evolving independently. A future round that
needs a fresher snapshot re-copies the file here in the same commit as
whatever manifest-entry change depends on the newer content, per this
document tree's owning-spec rule.
