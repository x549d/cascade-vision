# Changelog

## 0.3.0

- Add bounded, fresh-frame retries with backoff, non-retryable failures and pending-request suppression.
- Refresh successful verdicts by TTL/frame interval and periodically detect static frames.
- Export per-task JSONL events with usage, queue/service/e2e latency, lifecycle states and callback error counters.
- Expire stale queued tasks, cap total VLM attempts and isolate results across RTSP reconnect epochs.
- Reconnect RTSP with bounded consecutive attempts and FFmpeg open/read timeouts.
- Add annotated local-video comparisons for interval/motion/cascade/full policies, one-to-one episode scoring and paced event latency.
- Flag synthetic footage, simulation, incomplete clips and lossy runs; require explicit paid-benchmark opt-in and a shared attempt cap.
- Provide a synthetic fixture generator, real-video manifest, pilot feedback materials and a proposed six-month maintenance plan.

Behavior changes: periodic static detection and verdict refresh can increase API attempts. Use `--max-api-calls` or `max_vlm_calls` to cap requests. `get_verdict()` enforces TTL; raw cache access does not. No real-video validation or independent adoption is claimed by this release.

## 0.2.0

- Require explicit simulation; API errors, refusals and empty YOLO detections never become synthetic successes.
- Preserve response usage and report unknown totals; separate image estimates from actual tokens.
- Apply configured image detail, timeout and mock delay.
- Count stability across detector evaluations when raw frames are skipped.
- Retry after overflow and prevent stale asynchronous verdict replacement.
- Bound tracker state and cache; report failures, pending and cancelled work.
- Drain in-flight work within a total deadline and retain timed-out worker handles.
- Add JSON reports, terminal-first CLI, regression tests, CI and evaluation documentation.

Breaking: removed fixed token/cost savings fields and token constants; missing credentials fail outside mock mode; use a fresh pipeline for each stream. The CLI uses argparse and no longer patches Typer/Click globally.
