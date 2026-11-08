# cascade-vision — Detect Objects in Video and Describe Their Activity / 检测视频目标并描述其活动


[Chinese version](README.zh-CN.md) · [Download a release](https://github.com/x549d/cascade-vision/releases) · [Technical reference](docs/REFERENCE.md)

[![CI](https://github.com/x549d/cascade-vision/actions/workflows/ci.yml/badge.svg)](https://github.com/x549d/cascade-vision/actions/workflows/ci.yml)
[![MIT](https://img.shields.io/badge/license-MIT-green.svg)](LICENSE)

## Introduction

`cascade-vision` is a Python library and command-line tool that detects objects in video, selects objects for further analysis, and uses a vision-language model to describe their category and activity. It exports structured records that another program can read.

You can start with a self-contained offline demo, then connect a video file, webcam or RTSP stream. The project is early alpha: real-world accuracy and cost savings remain to be validated.

## What this project does

Use it to build a video-analysis prototype that turns selected images into object categories, activity summaries and event labels. For example, you can inspect selected people or bags in a clip, save the resulting descriptions, and compare how different frame-selection policies affect event recall and API usage.

The tool handles local motion filtering, object detection and tracking, background model requests, bounded failure recovery and event export. You can also compare fixed-interval sampling, motion filtering and the full cascade using annotated footage.

The main outputs are **a run summary** and **a stream of individual task results**. The default interface is a terminal and files; integrations can use the Python API.

## How to use it

### Try it without a camera, footage or API key

With Python 3.9 or newer, run:

```bash
git clone https://github.com/x549d/cascade-vision.git
cd cascade-vision
python -m pip install -e .
python -m cascade_vision.cli demo --mock-vlm --event-labels person,backpack --report-json reports/demo.json --events-jsonl reports/events.jsonl
```

The demo generates synthetic frames and uses a simple contour detector plus predefined mock descriptions. After dependency installation it sends no API requests and does not download detector weights. Mock descriptions demonstrate the output format, not recognition accuracy.

### Process your own video

Install the real detector, replace the path with a video you are authorized to process, and keep semantic analysis simulated while checking the pipeline:

```bash
python -m pip install -e ".[yolo]"
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

| File | What to look for |
| --- | --- |
| `reports/demo.json` | Run mode, processed frames, successful/failed tasks, observed tokens and timing |
| `reports/events.jsonl` | One JSON object per task, containing its status, object metadata, description and any known usage |

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

In the summary, inspect `failed_tasks`, `dropped_tasks`, `expired_tasks` and `budget_rejected_tasks` before interpreting throughput or filtering. `actual_tokens` uses returned API usage; it is zero for mock calls and can be `null` when complete usage is unknown. Cost estimates require configured prices and are not billing receipts.

For policy comparisons, open `reports/comparison/benchmark.json`. Read precision, recall and false alarms together with `quality_flags`. Synthetic and simulated runs are marked explicitly and have `comparison_ready: false`. Request timing includes queue/model work; event-to-verdict latency requires annotated, paced replay. Details are documented in the [scoring rules](docs/BENCHMARKS.md).

Further reading: [configuration and development](docs/REFERENCE.md), [contributing](CONTRIBUTING.md), [changelog](CHANGELOG.md), and [MIT license](LICENSE). Third-party models, packages and footage retain their own licenses.
