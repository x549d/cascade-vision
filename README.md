# cascade-vision — Cascaded Video Understanding / 级联视频语义分析

**Select video regions for VLM analysis with motion filtering and object tracking, then export structured events and compare sampling policies.**

**先在本地筛选画面、检测并跟踪目标，再按需交给视觉语言模型分析，导出结构化事件，并对比不同采样策略的效果与开销。**

[![CI](https://github.com/x549d/cascade-vision/actions/workflows/ci.yml/badge.svg)](https://github.com/x549d/cascade-vision/actions/workflows/ci.yml)
[![MIT](https://img.shields.io/badge/license-MIT-green.svg)](LICENSE)

[中文介绍](README.zh-CN.md) · [完整中文指南](docs/GUIDE.zh-CN.md) · [Latest release / 版本下载](https://github.com/x549d/cascade-vision/releases) · [Event schema / 事件接口](docs/EVENTS.md)

**Status / 状态：early alpha。** The library and CLI are runnable; real-video accuracy, cost savings and independent adoption remain to be validated. 已提供可运行的工具与离线测试，真实场景效果和成本节省仍待验证。

## Purpose / 项目作用

`cascade-vision` is a Python library and command-line tool for experimenting with selective video understanding. Use it to build a video-analysis prototype, decide when a frame or object crop needs semantic inspection, integrate per-request events, or compare the trade-offs between API usage, event recall and latency.

本项目面向视频分析原型开发与策略评测：帮助开发者决定哪些画面或目标区域需要进一步分析，接入逐条语义结果，并观察筛选策略对调用量、事件召回率和延迟的影响。

| Capability / 能力 | What it does / 作用 |
| --- | --- |
| Motion filtering / 运动筛选 | OpenCV MOG2 selects changing scenes, with periodic static-scene detection / 筛选有变化的画面，并支持静态画面定期检测 |
| Detection and tracking / 检测与跟踪 | YOLO + ByteTrack identifies objects and routes priority, uncertain or overlapping targets / 检测并跟踪目标，触发重点、疑难或重叠目标审查 |
| Selective VLM inspection / 按需语义分析 | Crops object regions for asynchronous structured inspection / 对目标区域异步分析，返回类别、活动和事件标签 |
| Failure recovery / 失败恢复 | Bounded retries, result refresh, queue expiry and RTSP reconnects / 有限重试、结果刷新、任务过期及 RTSP 重连 |
| Integration / 系统接入 | JSON summaries and JSONL task events with usage and timing / 导出汇总报告、逐条事件、用量和耗时 |
| Policy evaluation / 策略评测 | Compare fixed-interval, motion, cascade and full-frame policies on annotated clips / 在带标注片段上比较抽帧与级联策略 |

```text
Video / 摄像头 / RTSP
        -> Motion gate / 运动门控
        -> YOLO + ByteTrack / 目标检测与跟踪
        -> Trigger + crop / 触发与裁剪
        -> Bounded queue + VLM workers / 异步语义分析
        -> Structured verdicts + JSONL events / 结构化结论与事件
```

## Quick Start / 快速开始

Python 3.9 or newer. Run these commands from a terminal / 使用 Python 3.9 或以上版本，在终端执行：

```bash
git clone https://github.com/x549d/cascade-vision.git
cd cascade-vision
python -m pip install -e .
python -m cascade_vision.cli demo --mock-vlm --report-json reports/demo.json --events-jsonl reports/events.jsonl
```

The demo generates its own 300-frame synthetic video stream and uses a contour detector plus simulated VLM responses. After dependency installation it needs **no local footage, camera, model download, API key or paid API request**.

演示会自行生成 300 帧合成画面，使用轮廓检测和模拟语义结论。依赖安装完成后，**无需本地视频、摄像头、模型权重或 API 密钥，也不会产生 API 调用费用**。

| Output / 输出 | Contents / 内容 |
| --- | --- |
| `reports/demo.json` | Execution mode and aggregate metrics / 运行模式与汇总指标 |
| `reports/events.jsonl` | Per-task statuses, verdicts, usage and timing / 逐条状态、结论、用量与耗时 |

Mock verdicts are predefined examples; they do not establish real semantic accuracy. 模拟结论来自预设规则，不能据此判断真实识别效果。

## Examples / 使用示例

### 1. Offline Policy Comparison / 离线策略对比

```bash
python examples/generate_fixture.py --output-dir reports/fixture
python -m cascade_vision.cli benchmark --manifest reports/fixture/manifest.json --output-dir reports/comparison --mock-vlm --yolo-model mock
```

Compares interval, motion and cascade policies on the same generated clip. Inspect `reports/comparison/benchmark.json` and each policy's JSONL file. Synthetic footage and simulation are explicitly flagged. To speed up a smoke run, set `CASCADE_MOCK_LATENCY_S=0`.

对同一段合成视频运行固定间隔、运动筛选和完整级联策略，输出主报告与各策略的事件文件。报告会标明合成素材和模拟模式。快速验证时可设置 `CASCADE_MOCK_LATENCY_S=0`。

### 2. Local Video with Real YOLO and Mock VLM / 本地视频真实检测与模拟语义

```bash
python -m pip install -e ".[yolo]"
python -m cascade_vision.cli run --source local/video.mp4 --mock-vlm --headless --max-frames 300 --event-labels person,backpack --report-json reports/local.json --events-jsonl reports/local-events.jsonl
```

Replace the path with an authorized clip. YOLO weights may download on first use. Detection and tracking are real; VLM verdicts remain simulated and no VLM API request is sent.

将路径替换为有权使用的视频。首次运行可能下载 YOLO 权重；此模式进行真实检测与跟踪，语义结果仍为模拟，不发送 VLM API 请求。

### 3. Real VLM Inspection / 真实 VLM 语义分析

Configure `OPENAI_API_KEY` before running. This optional mode sends cropped images to your provider and can incur API charges. 设置密钥后可运行以下可选模式；它会发送裁剪图像，并可能产生 API 费用。

```bash
python -m cascade_vision.cli run --source local/video.mp4 --max-frames 300 --max-api-calls 10 --event-labels person,backpack --report-json reports/api.json --events-jsonl reports/api-events.jsonl
```

PowerShell: `$env:OPENAI_API_KEY = "your-key"`. Bash: `export OPENAI_API_KEY="your-key"`.

The attempt cap includes retries and is not a dollar ceiling. Reaching it is reported explicitly. 缺少密钥时真实模式会报错；次数限制包括重试，不等同于美元预算上限，达到限制会记录在报告中。

### 4. Python Integration / Python 接入

```python
from cascade_vision import CascadeConfig, CascadePipeline, JsonlEventWriter
from cascade_vision.cli import generate_synthetic_stream

config = CascadeConfig.from_env(
    mock_vlm=True,
    yolo_model="mock",
    event_labels=["person", "backpack"],
)

with JsonlEventWriter("reports/library-events.jsonl") as writer:
    pipeline = CascadePipeline(config, event_callback=writer.write)
    report = pipeline.run_stream(generate_synthetic_stream(), source_fps=30)
    print(report.to_dict())
```

Use a new pipeline per stream and `get_verdict(track_id)` for TTL-aware results. Lower-level `process_frame()` integrations must stop workers in a `finally` block. 每个视频流使用独立管线；读取有效结论时调用 `get_verdict()`。直接逐帧接入时，应在 `finally` 中关闭工作线程。

The default OpenCV package is headless. `--show-window` requires a suitable GUI environment and GUI-enabled OpenCV distribution. 默认使用无图形界面的 OpenCV；需要窗口显示时应单独配置 GUI 环境。

## Configuration / 参数配置

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

## Metrics and Limitations / 指标与局限

- `actual_tokens` comes from response `usage` (prompt + completion). Mock calls use zero real tokens. Missing usage, transport errors, or pending work make the complete total `null`; partial observed totals remain available.
- `estimated_cost_usd` uses your configured standard input/output rates. It is `null` when rates or complete usage are unavailable. It excludes caching discounts, special pricing and provider-side adjustments, and is not a billing receipt.
- Image-only estimates are separate hypothetical calculations. Low-detail bases are 2,833 for `gpt-4o-mini`, and 85 for `gpt-4o` / `gpt-4.1`. Unknown models, custom endpoints and high/auto detail return `null`. See [OpenAI token rules](https://developers.openai.com/api/docs/guides/images-vision#tile-based-image-tokenization).
- Failures, drops, cancellations, pending work and simulations are reported separately. Do not interpret them as successful filtering or accuracy improvements.
- FPS includes final queue drain. Service time, queue wait and task-to-verdict latency are distinct. Aggregate percentiles cover the last 4,096 attempts; JSONL supports exact offline statistics. Event-to-verdict latency is scored only during paced replay with annotations.
- Use `get_verdict(track_id)` to enforce cache TTL. Raw `verdict_cache` remains available for compatibility and can retain expired entries.
- RTSP reconnects clear cached/tracking state and advance a stream epoch; results from an old epoch cannot repopulate the cache. OpenCV's FFmpeg backend enforces the configured open/read timeout. Local EOF never reconnects.
- Static objects, camera movement, tracking gaps, small targets and cropped context can cause misses. This alpha is not validated for safety-critical decisions.

Version 0.2 removes the fixed `85/765` accounting fields and savings percentages. Migrate consumers to measured usage and the separately named estimates.

## Annotated Evaluation / 带标注对比评测

```bash
python examples/generate_fixture.py --output-dir reports/fixture
cascade-vision benchmark --manifest reports/fixture/manifest.json --output-dir reports/comparison --mock-vlm --yolo-model mock
```

This fixture is synthetic; reports explicitly mark that fact. For real clips, copy [the manifest template](examples/real-video-manifest.json), record footage permission, annotate event intervals and set `footage_type` accurately. Real provider evaluation requires `--allow-api` and a positive shared `--max-api-calls` cap. `--realtime` paces a local constant-frame-rate clip for event-to-verdict latency.

See [benchmark commands and scoring](docs/BENCHMARKS.md), [event schema/integration](docs/EVENTS.md), and [independent pilot guide](docs/PILOT_GUIDE.md). No real-video results or independent adoption are claimed yet.

## Development / 开发与测试

```bash
python -m pytest -q
python -m ruff check cascade_vision tests examples
python -m ruff format --check cascade_vision tests examples
python -m build
```

See [evaluation methodology](docs/BENCHMARKS.md), [contributing](CONTRIBUTING.md), and [changelog](CHANGELOG.md). There is no published real-video benchmark or claimed adoption count. Contributions with licensed footage, event annotations and raw measurements are welcome.

## License / 许可证

Project source is [MIT](LICENSE). Third-party packages, models and datasets retain their respective licenses.
