# Changelog

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
