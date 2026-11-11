# cascade-vision — Filter Video Before AI to Reduce Repeated API Requests / 视频先筛选再交给 AI，减少重复 API 请求


[Chinese version](README.zh-CN.md) · [Video workspace tutorial](docs/WEB.en.md) · [Download a release](https://github.com/x549d/video-ai-saver/releases) · [Technical reference](docs/REFERENCE.md)

[![CI](https://github.com/x549d/video-ai-saver/actions/workflows/ci.yml/badge.svg)](https://github.com/x549d/video-ai-saver/actions/workflows/ci.yml)
[![MIT](https://img.shields.io/badge/license-MIT-green.svg)](LICENSE)

## Introduction

`cascade-vision` is a local filtering layer between your video and a vision-language model, designed to reduce repeated AI API requests. It checks motion, detects and tracks objects, then sends selected cropped images to the model for further analysis. This lets you build a video-analysis workflow that submits selected observations instead of every frame.

The workflow is: **video → local motion filtering → object detection and tracking → rule-based selection → cropped images → AI analysis → structured records**.

It is a Python library and command-line tool for local video files, webcams and RTSP streams, with a self-contained offline demo. The project is early alpha: real-world semantic accuracy and net API cost savings still require validation.

## What this project does

Use it when you want AI to inspect selected video content while limiting repetitive image submissions:

- **Reduce repeated requests:** skip some static frames and use tracking, cooldowns and cached results to avoid repeatedly inspecting the same target. For example, a person appearing in hundreds of frames does not need a new model request on every frame; refresh and retry rules can still trigger later requests.
- **Send selected regions:** crop around a target and send that image to the model, rather than the entire frame. Whether a crop uses fewer image tokens depends on the model, image dimensions and detail settings.
- **Build an AI workflow:** export timestamped object categories, activity descriptions and event labels. Your own scripts or AI tools can use these records to prepare review lists, summarize selected observations, or feed a database or dashboard.

Here, “selected” means chosen by configurable object classes, confidence thresholds and trigger rules, such as a newly observed priority target, a low-confidence detection or overlapping targets. The system does not identify every meaningful moment, and filtering can miss content. It sends cropped still images to the model; it does not upload the complete video or automatically generate highlight clips.

The tool also handles background model requests, bounded failure recovery and event export. You can compare fixed-interval sampling, motion filtering and the full cascade on annotated footage to assess the tradeoff between event recall and API usage. Usage here means the connected model API's requests and returned tokens. Savings depend on the footage and configuration; fewer requests or smaller crops alone do not establish lower total cost.

The main outputs are **a run summary** and **a stream of individual task results**. Use the local browser workspace to review selected observations or the CLI and Python API to integrate the records into your own systems.

## How to use it

### Open the local review interface

Run these commands from the cloned repository.

```bash
python -m pip install -e ".[yolo]"
python -m cascade_vision.web
```

Open [http://127.0.0.1:8765](http://127.0.0.1:8765) in your browser. Import a video and select **开始分析** (Analyze). The workspace shows frame counts, motion filtering, API usage, and a filterable list of timestamped events. Select an event to inspect its bounding box or jump to that point in the original video. Download the run summary and event records from the same page.

The Chinese interface defaults to real local detection with simulated descriptions and makes no paid API calls. Select **运行离线演示** (Run offline demo) to explore it with synthetic footage. The demo also works without the YOLO extra. [Step-by-step tutorial with screenshots and AI export examples](docs/WEB.en.md)

### Try it without a camera, footage or API key

![Synthetic video demo with mock event output](https://raw.githubusercontent.com/x549d/video-ai-saver/main/docs/media/offline-demo.gif)

The animation shows moving targets in the synthetic clip, the mock events it produced, and the final summary for the JSON report and JSONL event log.

With Python 3.9 or newer, install from [PyPI](https://pypi.org/project/cascade-vision/) and run the offline demo.

```bash
python -m pip install cascade-vision
cascade-vision demo --mock-vlm --event-labels person,backpack --report-json reports/demo.json --events-jsonl reports/events.jsonl
```

The demo generates synthetic frames and uses a simple contour detector plus predefined mock descriptions. After dependency installation it sends no API requests and does not download detector weights. Mock descriptions demonstrate the output format, not recognition accuracy.

To install from source, clone this repository and run `python -m pip install -e .` from its root.

### Process your own video

Install the real detector, replace the path with a video you are authorized to process, and keep semantic analysis simulated while checking the pipeline:

```bash
python -m pip install "cascade-vision[yolo]"
python -m cascade_vision.cli run --source local/video.mp4 --mock-vlm --headless --max-frames 300 --event-labels person,backpack --report-json reports/local.json --events-jsonl reports/local-events.jsonl
```

This runs real detection and tracking; the semantic descriptions are still mock results. YOLO weights may download on first use. Use `--source 0` for a webcam or supply an RTSP URL for a stream.

For real semantic descriptions, configure `OPENAI_API_KEY` and remove `--mock-vlm`. For example:

```bash
python -m cascade_vision.cli run --source local/video.mp4 --max-frames 300 --max-api-calls 10 --event-labels person,backpack --report-json reports/api.json --events-jsonl reports/api-events.jsonl
```

Set the key in PowerShell with `$env:OPENAI_API_KEY = "your-key"`, or in Bash with `export OPENAI_API_KEY="your-key"`. This mode sends cropped images to the provider and can incur charges. The attempt cap includes retries and is not a dollar ceiling. Use the offline demo when no API budget is available.

### Compare frame-selection policies

```bash
python examples/generate_fixture.py --output-dir reports/fixture
python -m cascade_vision.cli benchmark --manifest reports/fixture/manifest.json --output-dir reports/comparison --mock-vlm --yolo-model mock
```

This compares the default policies on one generated, annotated clip. To evaluate real footage, follow the [benchmark guide](docs/BENCHMARKS.md); real API evaluation requires explicit opt-in and a shared attempt cap. Configuration and Python integration details are in the [technical reference](docs/REFERENCE.md) and [event interface guide](docs/EVENTS.md).

## How to read the results

The demo prints a summary in the terminal and writes two files:

| File | What it contains | What you can use it for |
| --- | --- | --- |
| `reports/demo.json` | Run mode, processed frames, task counts, observed tokens and timing | Check whether the run completed reliably and tune sampling, queue size or concurrency |
| `reports/events.jsonl` | Per-task status, time information, object metadata, descriptions and known usage | Build a review list, filter by event label, and feed records into your own storage or interface |
| `reports/comparison/benchmark.json` | Scores and operating metrics for each sampling policy, when a benchmark is run | Compare recall, false alarms, latency and usage on the same annotated footage |

For example, a completed **mock** record can contain the following fields. This is an abbreviated output example:

```json
{
  "status": "completed",
  "track_id": 1,
  "simulated": true,
  "applied": true,
  "verdict": {
    "is_valid_target": true,
    "refined_category": "courier",
    "activity_summary": "A courier is walking briskly across the monitored corridor carrying a parcel.",
    "risk_level": "NONE",
    "observed_events": ["person"]
  }
}
```

Read `refined_category` for the description's object category, `activity_summary` for the activity text, and `observed_events` for labels from your configured vocabulary. `simulated: true` identifies a predefined mock result. A failed or refused request does not become a successful mock result. `applied: false` means a completed response was too old or belonged to a previous stream epoch and was not used to update the cache.

### What the results are useful for

- **Review relevant moments:** use `media_timestamp_s` and `frame_idx` to locate an emitted observation in the original local video, then inspect its description and bounding box. This gives you a list of moments to review rather than a complete transcript of every frame.
- **Filter and organize observations:** use `observed_events`, `refined_category` and `activity_summary` to prepare label-based review lists or category counts. Repeated observations of one track are not automatically counts of unique people or separate real-world events.
- **Connect your own workflow:** parse the JSONL records or consume the Python callback, then send the selected data to your own database, dashboard or notification logic. Keep session, stream epoch and track ID together when grouping records.
- **Choose an operating policy:** compare labelled-video scores and measured usage to decide which sampling interval, retry limit and concurrency settings suit your footage. Inspect loss and failures alongside any apparent reduction in calls.

For example, this local script prints a review list of completed person observations from the event file generated above:

```python
import json
from pathlib import Path

with Path("reports/events.jsonl").open(encoding="utf-8") as source:
    for line in source:
        event = json.loads(line)
        if event.get("status") != "completed" or not event.get("applied", True):
            continue
        verdict = event.get("verdict", {})
        if verdict.get("is_valid_target") and "person" in verdict.get("observed_events", []):
            print({
                "time_s": event.get("media_timestamp_s"),
                "frame": event.get("frame_idx"),
                "description": verdict.get("activity_summary"),
                "simulated": event.get("simulated", False),
            })
```

The `simulated` field is retained so demo records remain visibly distinct from model observations. API usage and cost conclusions should use real responses; mock records are useful for checking the integration format.

In the summary, inspect `failed_tasks`, `dropped_tasks`, `expired_tasks` and `budget_rejected_tasks` before interpreting throughput or filtering. `actual_tokens` uses returned API usage; it is zero for mock calls and can be `null` when complete usage is unknown. Cost estimates require configured prices and are not billing receipts.

For policy comparisons, open `reports/comparison/benchmark.json`. Read precision, recall and false alarms together with `quality_flags`. Synthetic and simulated runs are marked explicitly and have `comparison_ready: false`. Request timing includes queue/model work; event-to-verdict latency requires annotated, paced replay. Details are documented in the [scoring rules](docs/BENCHMARKS.md).

Further reading: [configuration and development](docs/REFERENCE.md), [contributing](CONTRIBUTING.md), [changelog](CHANGELOG.md), and [MIT license](LICENSE). Third-party models, packages and footage retain their own licenses.
