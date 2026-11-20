# DownloadX adaptive engine validation plan

**Status:** required before the adaptive controller can be called production-ready.

This plan is deliberately separate from the existing IDM-vs-MDM benchmark. The historical
benchmark in `docs/bench/run5` measures an older MDM build against IDM; it does **not** compare
the adaptive controller with the pre-controller DownloadX engine. Its ratios must not be used
as evidence that adaptive concurrency improves DownloadX.

## What must be compared

Build two DownloadX revisions with the same Rust toolchain, target, feature set, profile and
machine configuration:

- **Baseline:** the parent revision immediately before the adaptive controller was integrated.
- **Adaptive:** the exact candidate commit containing the live Auto supervisor.

Keep all other source changes identical where practical. Record both full commit SHAs, lockfile
hashes, compiler version, Windows build number, CPU, RAM, storage model, power mode, network
adapter and driver, and the active security software. Run both binaries on the same machine and
lab server, with separate application-data directories and fresh destination directories.

The current `tools/bench/bench.py` is a Windows IDM-vs-MDM harness with a fixed MDM executable
path. It is **not yet an adaptive A/B harness**. Do not label its existing output as baseline vs
adaptive. Before collecting release evidence, extend or wrap the harness so each trial can select
the baseline or adaptive executable explicitly, launch it with an isolated data directory, and
record the selected commit and controller mode in every run record.

## Required scenario matrix

Use deterministic generated files from `mdm-labserver`. Run a warm-up for each binary/scenario,
then at least 10 measured trials per binary/scenario. Alternate trial order (baseline/adaptive,
then adaptive/baseline) to reduce thermal and time-order bias.

| Scenario | Required observation |
|---|---|
| Fast range-capable server | Throughput ceiling and controller overhead |
| Per-connection bandwidth cap | Whether additional workers improve aggregate throughput |
| High response latency | Whether ramp-up finds useful parallelism without overshooting |
| One periodically slow connection | Whether the controller avoids penalising the whole link for one slow worker |
| Server connection ceiling | Whether learned host caps remain respected |
| 429/503 throttling and transient failures | Fast reduction, backoff and eventual recovery |
| Dropped connections / resets | Correct retry and byte accounting during target changes |
| No range support (200 response) | One-stream fallback; no duplicate or missing bytes |
| Slow disk / disk backpressure | No false congestion reduction or unsafe worker churn |
| Per-download and global speed limit | Limits remain hard ceilings; no artificial ramp-up |
| Pause, resume and cancellation | No data loss, duplicate writes, orphaned workers or stuck part files |
| Small files | No needless connection churn or startup regression |

Where the lab server does not yet expose a required fault (notably a deterministic 429/503 with
Retry-After or controlled disk backpressure), add a focused fixture first rather than simulating
the condition in the controller test alone.

## Metrics to publish

For every scenario and revision, retain raw per-trial data and report:

- Successful completion count and exact final-size/checksum validation.
- End-to-end completion time and transfer-phase throughput.
- Median and p95 throughput, plus throughput coefficient of variation.
- Median and p95 completion time.
- Connection-count timeline (spawn, active, retirement requested, retired, respawned).
- Retry/error counts by class, throttled responses, bytes received and bytes committed.
- CPU time, peak working set and disk-held time.

Report medians, interquartile ranges and 95% bootstrap confidence intervals. Do not discard failed
trials: count and explain them. Compare paired trial order where possible. Keep raw logs and the
machine environment manifest beside the generated report.

## Acceptance gates

A candidate is eligible for release review only when all of the following are true:

1. Baseline and adaptive trials use the same machine, server fixtures, settings and build profile.
2. Every successful download has the expected length and checksum; no duplicate/overlapping
   committed bytes are observed.
3. No-range, host-cap, per-download/global limiter and disk-backpressure guardrails pass.
4. Pause/resume/cancel and worker-retirement race tests pass repeatedly on Windows, Linux and macOS.
5. Adaptive mode shows a statistically supported improvement in at least one declared target
   workload without a material regression in the other scenarios. If there is no improvement,
   tune or disable the controller; do not publish a speed claim.
6. CPU, memory, connection churn and failure rates remain within documented budgets.
7. Full CI, fuzzing, Windows installer build, install/upgrade/uninstall smoke tests and release
   artifact checks pass on the exact candidate commit.

Until these gates are met, the controller remains experimental. The existing default connection
policy and all hard safety ceilings must remain available as the fallback.

## Current evidence

- Cross-platform CI and fuzz checks passed on the original development candidate commit
  `e0cb955342f10eb186ea2570fb43c860b4cc86f9`.
- The adaptive integration test exercises a rate-limited range download through a sampling
  interval, cancellation, resume and byte-for-byte verification.
- Planner unit tests cover deferred retirement while a segment is owned, cancellation of a
  pending retirement and retirement after a released failed segment.
- These tests do not yet cover the complete supervisor race matrix listed above.
- No adaptive-vs-baseline benchmark result is currently available.
- Any documentation-only commits made after the CI candidate require their own CI run before
  they can be considered validated.

**Release statement:** no measured adaptive speed gain is claimed by this document.

## Pinned A/B revisions

The live supervisor was integrated by commit
`51745c12c5d83b526bde1036010cdbf0eed4237c`. The reproducible pre-supervisor baseline is
its direct parent:

- **Baseline:** `7deca2c4f83d73c6c39409a01fe6956e5807a603`
- **Adaptive candidate:** `39603bde90107d9c44334a5443814510a3ab07cb` (current PR head at the time
  this plan was updated; the installer/CI-only commits after the engine work do not change the
  adaptive implementation).

The baseline already contains the bounded telemetry and cooperative-retirement groundwork, but
does not contain the live adaptive supervisor. This makes it the intended before/after comparison
for the controller integration. The adaptive candidate also contains later branding, test,
documentation and installer changes; record both full SHAs and compare the engine source diff
before interpreting results. Do not describe this as a one-line-only controller benchmark.

### Windows worktree build preparation

From a clean clone of this repository, create two detached worktrees and build each in release
mode with the same installed Rust toolchain:

```powershell
git worktree add --detach ..\DownloadX-baseline 7deca2c4f83d73c6c39409a01fe6956e5807a603
git worktree add --detach ..\DownloadX-adaptive 39603bde90107d9c44334a5443814510a3ab07cb

Push-Location ..\DownloadX-baseline
cargo build --release --bin misterdownloadmanager --bin labserver
Pop-Location

Push-Location ..\DownloadX-adaptive
cargo build --release --bin misterdownloadmanager --bin labserver
Pop-Location
```

Keep the resulting executables in separate directories and pass their absolute paths to the A/B
runner. Do not copy one revision's executable over the other. Record `rustc -Vv`, `cargo -V`,
`git rev-parse HEAD`, and each worktree's `Cargo.lock` SHA-256 in the environment manifest.
Use separate `MDM_DATA_DIR` values and fresh destination folders for the two binaries.

The current IDM-vs-MDM script (`tools/bench/bench.py`) is not the A/B runner and must not be
used to produce adaptive-vs-baseline claims. The dedicated A/B runner must preserve the scenario
matrix, warm-up, alternating order, checksum validation, raw trial records and failure accounting
specified above. Until that runner and its results exist, benchmark validation remains pending.

