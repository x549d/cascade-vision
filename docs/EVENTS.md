# Event stream and integration

```bash
cascade-vision demo --events-jsonl reports/events.jsonl --report-json reports/run.json
cascade-vision run --source local.mp4 --event-labels person,backpack --events-jsonl reports/video.jsonl
```

JSONL contains one UTF-8 JSON object per line. The output file is replaced for each CLI invocation. Each record has `schema_version`, `session_id`, `timestamp_utc` and `status`.

Task records include `frame_idx` (1-based), `track_id`, `stream_epoch`, `media_timestamp_s` (0-based CFR file time; null for RTSP), `trigger_reason`, `attempt`, `bbox`, detector class/confidence and `completion_elapsed_s` relative to first-frame playback. Never infer a global identity from `track_id`; combine it with session and epoch.

| Status | Meaning |
| --- | --- |
| `completed` | Provider or explicit mock returned a valid verdict; includes `verdict`, `simulated`, `applied`, response usage and latency |
| `failed` | Provider failed/refused; includes error type, retryability and any known response usage |
| `dropped` | Queue admission failed; no provider request |
| `expired` | Queue age exceeded the deadline, or task belonged to an old stream epoch; no provider request |
| `budget_rejected` | Shared attempt cap reached; no provider request |
| `cancelled` | Queued work discarded at shutdown; no provider request |
| `stream_disconnected` / `stream_reconnected` | RTSP lifecycle record with new epoch; no source URL |

`queue_wait_ms` measures frame-to-admission/worker wait, including crop encoding. `service_ms` measures the provider call. `request_e2e_ms` measures task creation to completion, including queue wait. These measurements do not by themselves measure how long an event existed before its first sampled frame. Aggregate p50/p95 values cover the most recent 4,096 attempts; full records allow exact offline statistics.

`verdict.observed_events` uses the configured vocabulary. Failed, refused, expired and unsent tasks have no successful semantic prediction. An old response can complete with `applied=false`: it still counts toward provider usage but must not overwrite a newer/epoch-current result.

## Library callback

```python
from cascade_vision import CascadeConfig, CascadePipeline, JsonlEventWriter

with JsonlEventWriter("reports/events.jsonl") as writer:
    pipeline = CascadePipeline(CascadeConfig.from_env(), event_callback=writer.write)
    pipeline.run_stream("local.mp4")
```

Callbacks run on producer and worker threads; custom callbacks must be thread-safe and fast. Export failures increment `event_export_errors`; CLI exits unsuccessfully when data is lost. `JsonlEventWriter` synchronizes and flushes each line. Raw ROI payloads, API keys, source URLs and provider exception messages are omitted. Semantic text can still describe people in the footage: review event files before sharing them.

`get_verdict(track_id)` enforces the configured TTL. Direct access to `verdict_cache` is retained for compatibility but can contain expired entries; use `get_verdict()` in integrations.
