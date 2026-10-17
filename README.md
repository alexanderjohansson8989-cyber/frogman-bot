# Frogman-bot: candidate v1

Development snapshot of a forecasting core, not a deployed competition bot.
The provisional profile uses Opus High for research and forecasting, with a
separate Opus Medium review. Model availability and prices must be checked
before connecting a real transport. No winning-performance claim is made.

## Run the offline tests

Python 3.12 or newer:

```sh
python run_tests.py
```

The test runner blocks network connections and needs no API keys or external
packages. All account, question and forecast examples used by these tests are
synthetic. The numeric-format fixture intentionally uses invalid empirical-like
CDFs; it does not redistribute a real question or research dataset.

## Code map

- `work/candidate_service_v1.py`: one-question orchestration to a local plan.
- `work/candidate_pipeline_v1.py`: research, source capture, forecast and review.
- `work/candidate_profile_v1.py`: model settings and evidence instructions.
- `work/research_assessment_v1.py`: complete-response and source-span checks.
- `work/operations_ledger_v1.py`: SQLite reservations, sessions and stop states.
- `work/git_state_store_v1.py`: private Git snapshots and ownership fencing.
- `work/github_git_transport_v1.py`: bounded Git-data transport for checkpoints.
- `work/durable_candidate_host_v1.py`: full request/source archives, checkpointed
  transactions and question/credit/key expiry checks with injected model I/O.
- `work/candidate_recovery_v1.py`: explicit recovery after the owning GitHub
  workflow completed; saved usage and server readback only, no new model call.
- `work/candidate_api_v1.py` and `work/candidate_dispatch_v1.py`: fixed HTTP
  endpoints and persisted single-use dispatch permits. No automatic retries.
- `work/candidate_comments_v1.py`: a separate private-comment transaction,
  disabled by default and requiring an identical verified forecast first.
- `work/candidate_dryrun_v1.py`: a single-use, frozen-question pilot entry point
  with balance/time/version guards; it never submits forecasts or comments.
- `work/competition_adapter_v1.py`: pure question binding, API formatting and
  an opt-in submission driver exercised with simulated transports only.
- Other modules are inherited helpers included to make the import closure
  inspectable. Their presence does not mean every historical experiment is an
  active production path. The exact tested modules are listed in `run_tests.py`.

## Deployment status

Production competition operation and scheduling are not activated. The service
produces a local frozen submission plan. HTTP clients exist, but effectful calls
require a persisted single-use permit; submission and comments are disabled by
default. No live forecast or comment has been submitted by this version.
A separate diagnostic module can make
authenticated GET requests. The explicitly invoked storage proof can create a
private Git test branch containing synthetic data, using a GitHub job token.
The offline tests replace all those transports with mocks.

The durable host snapshots SQLite and full request/source records into a private
Git state branch before external effects, validates content hashes and fences
interrupted owners. Expiry times must be configured explicitly. This remains a
development wrapper with explicitly configured transports. A recovery wrapper
can take ownership only after the original workflow is verified completed; it
cannot dispatch new model/forecast/comment writes. Real deployment policy,
whole-chain live verification and live submission verification still need
integration. The storage proof uses synthetic
state, not a production credit allocation. A temporary CI disk is not durable.

The code must never infer that a timeout means a forecast was not received.
Uncertain requests retain reservations and stop further work. Retrieved source
text is evidence, not authority to alter budget, permissions or instructions.

## Dependencies and provenance

The tested code uses the Python standard library. PDF extraction additionally
requires `pdfplumber`; it is optional and not installed by the test runner.
No external dependency source is bundled. API format references appear in the
relevant source modules; they are not a claim that the upstream project
endorses this implementation.

`SOURCE_MANIFEST.json` records source hashes and the earlier CI snapshot.
Production Python files are byte-identical to that snapshot. The two numeric
test fixtures and the corresponding test name were changed for this package;
the complete offline suite is rerun on the resulting package separately.

This package excludes private project notes, credentials, run logs, raw model
responses and collected research sources. It is a local publication candidate;
the existing GitHub repository and its historical contents require a separate
publication review. No distribution license has been selected for this draft.
