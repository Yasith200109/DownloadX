# DownloadX speed engine: implementation and validation plan

This document is the working engineering contract for the DownloadX multi-connection work.
It deliberately starts from the existing engine rather than replacing its planner or retry logic.

## Existing behaviour to preserve

- HTTP/HTTPS and FTP/FTPS transfers use the Rust engine in `crates/engine`.
- HTTP downloads use one client/pool per download and one in-flight request per worker.
- Range support is confirmed by a valid partial response; servers that ignore ranges fall back to one stream.
- `Options::max_connections` defaults to 8 and is clamped to 1..=128 (the normal UI ceiling is 32).
- The planner assigns, preempts and splits byte ranges while preventing overlapping writes.
- Slow-connection shedding is opt-in through `Options::shed_slow` (enabled by default), and is suppressed when a speed limiter, request cap or disk backpressure makes its measurements misleading.
- Per-host connection limits, retry handling, resume validators and overlap verification must remain intact.

## DownloadX speed modes

The user-facing mode is a policy layered over the existing engine. It must not bypass server capability detection or host limits.

### Auto (default)
- Start conservatively using the user's configured connection ceiling and the engine's file-size / host-cap checks.
- Increase parallelism only after measured throughput improves without a sustained rise in RTT, retry rate or timeout rate.
- Decrease parallelism quickly after congestion, repeated throttling or connection failures; increase it slowly after a stable interval.
- Keep the current slow-worker recovery, but do not shed workers while the disk or an explicit speed limit is the bottleneck.

### Maximum Speed
- Permit faster additive increases up to the configured ceiling when the server accepts ranges and the network is not congested.
- Retain hard limits from the server, host-limit learning, memory budget, disk backpressure and the user's global/per-download limits.
- Never claim speed beyond the server, route, Wi-Fi/mobile link or disk capacity.

### Stable Speed
- Prefer fewer simultaneous workers and longer observation windows.
- Penalize retry bursts, RTT spikes and short-lived throughput gains.
- Avoid frequent connection churn; keep a slow worker if reconnecting is unlikely to improve completion time.

## Telemetry required before changing concurrency

Collect per-download, bounded-window measurements:
- aggregate bytes/sec (smoothed and instantaneous);
- per-worker bytes/sec and time spent receiving vs waiting for disk;
- request RTT / time-to-first-byte;
- active, receiving, retrying and retired worker counts;
- HTTP status / transport error counts, especially 429, 503, timeout and reset;
- server range support and learned per-host connection cap;
- disk write / flush wait signals and limiter state.

Telemetry must be bounded, cheap, and reset when a new download starts. Do not persist cookies, authorization headers, or full signed URLs in diagnostics.

## Adaptive controller guardrails

- Use a smoothed throughput estimator (EWMA) for display and a separate short-window signal for congestion detection.
- Use additive increase and multiplicative decrease with hysteresis; never change the worker count on every UI refresh.
- Require multiple healthy observation windows before increasing concurrency.
- Reduce concurrency on repeated 429/503, rising RTT plus falling throughput, or repeated connect/read timeouts.
- Do not interpret a single slow worker as whole-link congestion.
- Do not increase workers when ranges are unsupported, a host cap is known, a limiter is active, or disk backpressure is active.
- Never exceed the configured user ceiling or `MAX_CONNECTIONS_UNLOCKED`.
- Controller decisions must be deterministic under a fake clock / synthetic telemetry so they can be unit-tested.

## Benchmark matrix and acceptance criteria

Compare the unmodified baseline with each controller mode using identical server fixtures and file sizes.

| Scenario | What it checks |
| --- | --- |
| Local fast range server | Maximum throughput and CPU overhead |
| Per-connection server cap | Whether extra workers help or just add overhead |
| High RTT / low loss | Slow ramp and useful parallelism |
| Injected packet loss / resets | Recovery and retry stability |
| Periodically slow worker | Avoiding whole-download stalls |
| 429 / 503 with Retry-After | Backoff and server friendliness |
| No Range support / server returns 200 | Correct single-stream fallback |
| Range server returns malformed 206 / 416 | No corrupt or overlapping writes |
| Slow disk / long flush | No false congestion response |
| Pause, resume and process restart | Correct validators and byte continuity |
| Network changes during transfer | Recovery without duplicate writes |

Record median throughput, p95 throughput, speed coefficient of variation, completion time, retry count, failed requests, CPU and peak memory. A change is accepted only when correctness tests pass and it improves the target mode without a material regression in the other modes.

## Rollout order

1. Add deterministic controller and telemetry unit tests without changing defaults.
2. Add the controller behind an explicit internal feature flag and compare against the existing supervisor.
3. Run the benchmark matrix and tune thresholds from results, not guesses.
4. Expose Auto / Maximum Speed / Stable Speed in the app settings and per-download controls.
5. Enable Auto by default only after Windows CI and benchmark results are clean.

## Branding and compatibility notes

- Keep the existing installer AppId, mutex, registry keys, app-data directory and browser native-messaging identifiers during the first branding rollout. Existing installations and browser integrations depend on them.
- Keep original upstream attribution and GPL-3.0-or-later notices.
- Rename executable and release artifact filenames only in one coordinated change with the updater, installer, smoke tests, browser host manifests, CI and release workflows. A partial rename can make upgrades or browser integration fail.

## Live-supervisor integration audit (2026-11-19)

The first live integration must not be implemented as a simple comparison between worker IDs and a desired count.

- `supervise()` currently launches workers into a Tokio `JoinSet` and assigns monotonically increasing `WorkerId` values.
- A worker repeatedly asks the planner for its next assignment. Returning from a worker is currently tied to planner completion, range capability, retry exhaustion, or errors; there is no independent per-worker retirement signal.
- The current cancellation token is shared by the whole download. Cancelling it to reduce parallelism would stop every worker, not just excess workers.
- Worker IDs are identifiers for planner ownership and progress views, not reusable concurrency slots. A target-count check against `next_id` would therefore be incorrect after workers retire.
- The planner already exposes active and receiving worker counts, while `ConnView` exposes per-worker speed and status. The supervisor still needs bounded request-latency / response-outcome telemetry before the policy can safely react to network congestion.
- App settings are serialized with `#[serde(default)]`, and already persist the configured connection ceiling. A future mode preference can be added compatibly, but it should not be surfaced until the live engine actually consumes it.

### Cooperative retirement protocol to implement

The safe retirement point is the worker loop boundary after a complete segment, not an arbitrary point in an HTTP response.

1. Give each spawned worker its own retirement signal, held by the supervisor in a registry keyed by its `WorkerId`. Keep the download-wide cancellation token separate; it remains reserved for pause/cancel/complete.
2. Have the `JoinSet` task return its `WorkerId` with the worker result so the supervisor can remove exactly the completed worker from the registry. Never treat `next_id` as the number of active workers; IDs remain monotonically increasing.
3. A worker checks its retirement signal before asking the planner for another assignment. A worker already inside `receive_all` is allowed to finish its current segment. `Writer::wait` has then awaited the disk write and called `Planner::commit`; the worker can return without abandoning buffered bytes or releasing an uncommitted claim.
4. If a worker is between segments after a preemption or retryable error, the existing path releases its planner claim before the next loop iteration; it can then retire at that boundary. Do not abort its task or cancel its HTTP body as a shortcut.
5. When the target falls, mark only the excess live workers for retirement (prefer the newest non-probe workers). When the target rises, spawn fresh workers with new IDs until the live-task count reaches the target. The supervisor continues to own respawn, host caps, range gating, file-size limits and the final completion check.
6. Keep the probe/connection-0 fallback invariant: no-range servers must continue with the original whole-file stream. A controller target of one must never cancel or retire the only worker that can finish a no-range download.
7. On global cancellation or completion, retain the existing shared-token shutdown path and await every worker. A retirement signal must never mask a fatal error or turn a cancelled download into success.

### Telemetry still required for a live controller

- Count actual response outcomes at the request boundary, including status 429/503, transport failures, timeouts and Retry-After; supervisor task errors alone miss retries handled inside a worker.
- Measure request RTT/TTFB per response. `Snapshot.server.rtt` is a connection-open estimate, not a rolling per-request RTT signal, so it must not be misused as congestion telemetry.
- Sample aggregate received bytes over a monotonic bounded window; exclude windows in which `disk_held` says writes constrained the network.
- Derive active task count from the supervisor's live-worker registry, not planner ownership alone (a worker may be assigned while retrying).
- Treat unsupported ranges, known host caps, per-download/global limiters and disk backpressure as hard guardrails before applying any target.

### Retirement regression tests

Before enabling the controller, cover: retirement while receiving a segment; retirement while a disk write is pending; retirement after a preempted claim; retry then retirement; pause/cancel racing with retirement; final segment completion racing with target reduction; worker respawn after a prior worker exits; no-range probe preservation; resume-state byte continuity; and a many-worker randomized planner test proving every byte is committed at most once.

**Integration gate:** implement the protocol and telemetry above, then run the engine suite, app tests, Windows/macOS/Linux builds, fuzzing and the benchmark matrix. Until those checks pass, the controller remains a tested policy module and must not be advertised as active download behaviour.
