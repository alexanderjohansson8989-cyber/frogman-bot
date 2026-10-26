# Frogman-bot – development candidate, 26 October 2026

Readable code corresponding to locally tested bundle dc4f3d757fd543e9734920f17a555d37c617364e7e2bd2af4f2b883ad95eef4f.
Production source bytes are unchanged. SOURCE_MANIFEST records separate remote CI evidence when available; null means not verified.
This is not an activated competition deployment or a claim of winning accuracy.

## Pipeline

The candidate uses Opus High for research/forecasting and Medium for one need
assessment and one factual review. Research selects search results and literal
HTTPS links from resolution criteria. Captured text is untrusted evidence.
Follow-up source reads are bounded. Gap labels are tentative diagnostics, never
facts or authority for extra searches. Forecasting sees captured source text,
not the need assessor's unsupported factual hints. Numeric/discrete distributions
are projected to API constraints with distortion/tail diagnostics recorded.
Review separates verified factual objections, subjective future uncertainty and
technical unavailability. Unavailability does not claim factual approval.

The service stores a frozen submission plan. SQLite reservations and single-use
permits are persisted through private Git snapshots before external effects.
Interrupted/uncertain operations require bounded readback recovery. Publication
intent allows a pending private comment to continue after a verified forecast
without sending that forecast again. Completed state is compacted with archives
preserved. The queue runs recovery before intake. Outcome metrics describe the
returned service/queue snapshot, not season coverage or accuracy.

## Entry points and safeguards

- `work/production_entry_v1.py`: disabled preparation-only entry, no bootstrap.
- `work/publication_entry_v1.py`: disabled bounded publication cycle; exact code,
  configuration, known workflow commit and one-use nonce required.
- `work/candidate_pipeline_v1.py` and `candidate_profile_v1.py`: model process.
- `work/research_assessment_v1.py`: source-bound findings and optional gap labels.
- `work/operations_ledger_v1.py`, `git_state_store_v1.py` and
  `durable_candidate_host_v1.py`: financial/ownership/attempt controls.
- `work/production_cycle_v1.py` and `publication_intent_v1.py`: queue and recovery.
- `work/outcome_metrics_v1.py` and `numeric_diagnostics_v1.py`: passive diagnostics.

The current update validates explicit probability bounds before factual review,
keeps long source URLs in evidence while omitting them with a hash note in the
short comment, and includes cleaned criteria text in excerpt relevance terms.
Publication nonce consumption has a named audit event in the same transaction.
Recovery may stop before nonce consumption; this is not permission to retry.

No active workflow, credentials, launch configuration or state database ships.
Account/repository identifiers and historical test-driver identifiers are code
constants, not tokens. Historical helper entry points remain for inspection and
offline regression; their presence is not authorization to execute them.
API keys must be supplied privately, never committed. Code rotation on an active
state branch requires a preserving migration; automatic reset is prohibited.

## Offline tests

Run `python run_tests.py` with Python 3.12+. The runner blocks network connections,
uses a local temporary directory and needs no credentials. Its manifest lists
the suite. Private historical source/acceptance fixture tests are explicitly
skipped; synthetic numeric fixtures replace historical data. This public-package
test result is distinct from the private local package: 423 passing tests plus one skip.
No external dependency sources are bundled. PDF capture optionally requires
`pdfplumber`, not needed for these tests. No distribution license is selected.

`SOURCE_MANIFEST.json` records original hashes, the three test-only adjustments
and the exact delivered bytes. Private notes, model responses, scraped research,
run logs, journal history and private JSON fixtures are excluded. This is a local
candidate until a separate upload is verified; it does not update GitHub itself.
