# Annotated video evaluation

No real-video results are published yet. The runner provides reproducible comparisons, not a claim that a strategy saves money at equal recall.

## Offline smoke comparison

```bash
python -m pip install -e ".[dev]"
python examples/generate_fixture.py --output-dir reports/fixture
cascade-vision benchmark --manifest reports/fixture/manifest.json --output-dir reports/comparison --mock-vlm --yolo-model mock
```

For a fast smoke run, set `CASCADE_MOCK_LATENCY_S=0`. A larger `CASCADE_MAX_QUEUE_SIZE` can avoid queue loss when running unpaced. The generator uses seed 42, MJPG, 30 FPS, 640x480 and 300 frames. It writes synthetic footage plus event annotations. Reports mark `synthetic_footage` and `simulation`; they cannot validate real YOLO/VLM accuracy or API savings.

## Authorized real-video comparison

1. Copy `examples/real-video-manifest.json` next to your constant-frame-rate clip. Set the relative path, record license/permission, declare `footage_type: real` and replace the example annotation with actual observable intervals.
2. Install `.[yolo]`, set your provider key, select an explicit model, and configure standard input/output prices if you want cost estimates. Values are not assumed from historical pricing.
3. Run a small, explicitly capped evaluation:

```bash
cascade-vision benchmark --manifest local/manifest.json --output-dir reports/real --strategies interval,motion,cascade --allow-api --max-api-calls 100 --realtime
```

The limit of 100 here is an example **total attempt cap** shared by all clips and strategies, including failed calls/retries. It is not a dollar ceiling. Choose a cap appropriate to your own budget. If earlier strategies exhaust it, later runs are flagged and the comparison is incomplete. Use a separate output directory for each repetition; files in the same directory are replaced.

Use `--strategies full,interval,motion,cascade` for an optional every-frame baseline. This can create substantial API usage. `--realtime` paces decoded frames using CFR video timestamps; only paced replay produces event-to-verdict latency scores. Unpaced runs measure throughput and request latency, with event latency left null.

All policies share the model, image detail, event vocabulary and system prompt. Interval/motion/full send full frames; cascade sends detector-selected crops and includes object metadata. These input differences are intrinsic to the policies and must be disclosed. The default motion/interval policies do not require detector predictions for their requests; initialization still loads the configured detector, so configure it consistently and exclude initialization from throughput interpretation.

## Manifest

Root fields: `schema_version: 1`, `event_labels` (unique strings), `clips` (non-empty list). Each clip has a unique safe `id`, local `path`, `license`, `footage_type` (`real` or `synthetic`), and `events`. Each event has a unique `id`, vocabulary `label`, and finite `0 <= start_s < end_s <= duration`. Paths are relative to the manifest. No automatic downloads occur.

Annotations describe event presence in the clip, not spatial localization or object identity. The current scorer cannot evaluate tracking accuracy or complex multi-frame interactions.

## Event scoring

- Use only completed, applied, valid verdicts with `observed_events` from the configured vocabulary.
- Group predictions by label and merge successive points separated by at most `--merge-gap-s` (default 1 second).
- Match predicted episodes to annotations one-to-one, maximizing match count. A match requires the same label and an actual sampled point inside the annotation; bridging a prediction gap alone is insufficient.
- Report event TP/FP/FN, precision, recall, F1 and false alarms per hour. Undefined metrics are null, not perfect scores.
- During paced replay, event-to-verdict delay is the earliest completion time of an in-interval point minus annotation start. It includes capture/selection delay and queue/provider time. Report p50/p95 over matched events, alongside recall so missed events remain visible.

Merging tolerance changes episode counts and can merge separate real events. Keep it fixed across runs and publish it with annotations. Scores require manual annotation quality checks; a successful command does not establish scientific validation.

## Outputs and validity

`benchmark.json` records version/source commit when available, working-tree dirty status, runtime/dependency versions, platform, non-secret configuration, clip hashes/dimensions/FPS/duration, scores and metrics. Each strategy writes a raw JSONL event file. Source paths, source URLs, keys, endpoint URLs and images are omitted from reports. Hardware GPU details and measurement dates/rates should also be recorded in your published experiment notes.

Loss, failures, missing usage, exhausted request budgets, incomplete clips, simulation and synthetic footage appear in `quality_flags`. `comparison_ready` is false for these cases. The CLI returns a failure status for operational problems; synthetic/simulation flags alone are expected for smoke runs. `scientific_claims_validated` stays false because independent validation and annotation review cannot be inferred automatically.

Publish licensed dataset provenance, exact source commit, configuration, raw sanitized logs, annotations and repeated measurements. Do not publish private footage, identifying descriptions or credential-bearing URLs without permission. Negative results and failure cases are part of the evaluation.
