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
