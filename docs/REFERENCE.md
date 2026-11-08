# Technical reference

Configuration, operational behavior and development commands for cascade-vision. For an introduction, see the [project homepage](../README.md).

## Configuration

`CascadeConfig.from_env()` accepts overrides and `CASCADE_<FIELD>` environment variables, as well as `OPENAI_API_KEY` and `OPENAI_BASE_URL`. Constructor overrides take precedence.

| Field | Default | Purpose |
| --- | --- | --- |
| `motion_ratio_threshold` | `0.012` | Foreground area needed to run the detector |
| `yolo_model` | `yolo11n.pt` | YOLO weights, or explicit `mock` |
| `conf_min` / `conf_ambiguous_upper` | `0.25` / `0.55` | Detection floor and ambiguous confidence band |
| `min_stable_frames` | `3` | Consecutive detector observations before first priority audit |
| `cooldown_frames` | `90` | Raw-frame interval between audits of the same track |
| `max_track_age_frames` | `300` | Forget state after this many unseen raw frames |
| `occlusion_iou_threshold` | `0.35` | Overlap heuristic; does not prove interaction |
| `roi_padding_ratio` | `0.15` | Context around an object crop |
| `openai_model` / `vlm_detail` | `gpt-4o-mini` / `low` | Model and image detail |
| `api_timeout_s` | `10` | Provider timeout; SDK automatic retries disabled |
| `max_queue_size` / `worker_threads` | `20` / `2` | Queue capacity and consumer count |
| `max_cached_tracks` | `1000` | Bound the verdict cache |
| `audit_max_retries` | `2` | Fresh-frame retry limit after the first failed attempt |
| `retry_backoff_s` / `retry_backoff_max_s` | `1` / `30` | Exponential retry delay and cap |
| `verdict_ttl_s` | `60` | Cache validity and time-based refresh |
| `refresh_interval_frames` | `300` | Refresh successful tracked targets; 0 disables frame-based refresh |
| `detector_refresh_interval_frames` | `30` | Detect even on static frames; 0 disables periodic detection |
| `max_task_age_s` | `10` | Expire queued tasks before sending stale crops |
| `rtsp_reconnect_attempts` | `3` | Maximum consecutive reconnect attempts after RTSP failure |
| `capture_timeout_ms` | `5000` | FFmpeg RTSP open/read timeout |
| `max_vlm_calls` | `0` | Total attempts cap; 0 means unlimited |
| `strategy` / `sample_interval_s` | `cascade` / `1` | Comparison policy and interval sampling period |
| `event_labels` | `[]` | Observable event vocabulary for annotation scoring |
| `mock_vlm` / `mock_latency_s` | `False` / `0.30` | Explicit simulation and service delay |
| `input_cost_per_million_tokens` | unset | Optional user-supplied input rate |
| `output_cost_per_million_tokens` | unset | Optional user-supplied output rate |

Stability counts consecutive **detector evaluations**, allowing raw frames to be skipped by the motion gate. It resets on absence in a detector evaluation or track expiry. Dropped tasks release their cooldown for retry. Transient failures retry after bounded backoff on a fresh observed frame, bypassing the normal cooldown. Non-retryable responses (including refusals/auth errors) and exhausted retry cycles stop until the track expires or you call `tracker.clear_audit_failure(track_id)`. Pending tasks suppress duplicate requests. Successful results refresh after the frame interval or TTL, subject to the normal cooldown. Overlapping objects are inspected as individual crops; temporal interaction reasoning is not implemented.

## Metrics and Limitations

- `actual_tokens` comes from response `usage` (prompt + completion). Mock calls use zero real tokens. Missing usage, transport errors, or pending work make the complete total `null`; partial observed totals remain available.
- `estimated_cost_usd` uses your configured standard input/output rates. It is `null` when rates or complete usage are unavailable. It excludes caching discounts, special pricing and provider-side adjustments, and is not a billing receipt.
- Image-only estimates are separate hypothetical calculations. Low-detail bases are 2,833 for `gpt-4o-mini`, and 85 for `gpt-4o` / `gpt-4.1`. Unknown models, custom endpoints and high/auto detail return `null`. See [OpenAI token rules](https://developers.openai.com/api/docs/guides/images-vision#tile-based-image-tokenization).
- Failures, drops, cancellations, pending work and simulations are reported separately. Do not interpret them as successful filtering or accuracy improvements.
- FPS includes final queue drain. Service time, queue wait and task-to-verdict latency are distinct. Aggregate percentiles cover the last 4,096 attempts; JSONL supports exact offline statistics. Event-to-verdict latency is scored only during paced replay with annotations.
- Use `get_verdict(track_id)` to enforce cache TTL. Raw `verdict_cache` remains available for compatibility and can retain expired entries.
- RTSP reconnects clear cached/tracking state and advance a stream epoch; results from an old epoch cannot repopulate the cache. OpenCV's FFmpeg backend enforces the configured open/read timeout. Local EOF never reconnects.
- Static objects, camera movement, tracking gaps, small targets and cropped context can cause misses. This alpha is not validated for safety-critical decisions.

Version 0.2 removes the fixed `85/765` accounting fields and savings percentages. Migrate consumers to measured usage and the separately named estimates.

## Annotated Evaluation

```bash
python examples/generate_fixture.py --output-dir reports/fixture
cascade-vision benchmark --manifest reports/fixture/manifest.json --output-dir reports/comparison --mock-vlm --yolo-model mock
```

This fixture is synthetic; reports explicitly mark that fact. For real clips, copy [the manifest template](../examples/real-video-manifest.json), record footage permission, annotate event intervals and set `footage_type` accurately. Real provider evaluation requires `--allow-api` and a positive shared `--max-api-calls` cap. `--realtime` paces a local constant-frame-rate clip for event-to-verdict latency.

See [benchmark commands and scoring](BENCHMARKS.md), [event schema/integration](EVENTS.md), and [independent pilot guide](PILOT_GUIDE.md). Real-video accuracy and cost trade-offs require evaluation on authorized annotated footage.

## Development

```bash
python -m pytest -q
python -m ruff check cascade_vision tests examples
python -m ruff format --check cascade_vision tests examples
python -m build
```

See [evaluation methodology](BENCHMARKS.md), [contributing](../CONTRIBUTING.md), and [changelog](../CHANGELOG.md). Real-video benchmark results have not been published. Contributions with licensed footage, event annotations and raw measurements are welcome.

## License

Project source is [MIT](../LICENSE). Third-party packages, models and datasets retain their respective licenses.
