# cascade-vision — Filter Video Before AI to Reduce Repeated API Requests / 视频先筛选再交给 AI，减少重复 API 请求


[英文版](README.md) · [版本下载](https://github.com/x549d/cascade-vision/releases) · [完整中文指南](docs/GUIDE.zh-CN.md)

## 简介

`cascade-vision` 是放在视频和视觉语言模型之间的一层本地筛选工具，目标是减少重复的 AI API 请求。它先检查画面运动、检测和跟踪目标，再把选中的目标区域裁剪成图片交给 AI 分析，帮助你搭建按需提交画面的视频分析流程。

流程是：**视频 → 本地运动筛选 → 目标检测与跟踪 → 按规则挑选 → 裁剪目标区域 → AI 分析 → 输出结构化记录**。

项目提供 Python 接口和命令行工具，支持本地视频、摄像头和 RTSP 视频流，可以先运行自带的离线演示。项目目前处于早期 alpha 阶段，真实场景的语义准确率和净 API 成本节省仍需验证。

## 项目有何作用

如果你希望让 AI 分析视频，又希望控制重复提交画面带来的 API 用量，可以用它来：

- **减少重复请求：** 跳过部分静止画面，结合目标跟踪、冷却时间和缓存，避免反复分析同一目标。例如，同一个人在视频中出现几百帧，不必每一帧都重新请求 AI；后续仍可按刷新或重试规则再次提交。
- **只提交选中的区域：** 将目标附近的画面裁剪成图片交给 AI，缩小提交范围。裁剪后是否减少图像 token，取决于模型、图像尺寸和细节设置。
- **搭建自己的 AI 工作流：** 导出带时间戳的目标类别、活动描述和事件标签，交给自己的脚本或 AI 工具，整理待复核清单、汇总选中时刻的观察结果，或接入数据库与看板。

这里的“重点”由可配置的目标类别、置信度和触发规则决定，例如新出现的优先目标、低置信度目标或目标重叠。它不能保证发现所有有意义的时刻，筛选也可能漏掉内容。交给 AI 的是裁剪后的静态图片；当前不直接上传整段视频，也不自动生成精彩片段。

程序还负责后台模型请求、有限的失败恢复和事件导出。你可以用带事件标注的视频，对比固定间隔抽帧、运动筛选和完整级联，评估事件召回率与 API 用量之间的取舍。这里的用量指所接入模型服务的 API 请求和响应返回的 token。节省效果取决于素材和配置，不能仅凭请求次数或裁剪尺寸减少，就认定总成本下降。

最终主要得到两类输出： **本次运行的汇总报告**和**逐条任务结果**。你可以将选中画面的观察结果整理成待复核清单、可筛选记录，或作为自己系统的数据输入。默认通过终端和文件使用，也可以通过 Python 接口接入其他系统。

## 怎么用

### 没有摄像头、视频和密钥，先运行离线演示

使用 Python 3.9 或更高版本，在终端执行：

```bash
git clone https://github.com/x549d/cascade-vision.git
cd cascade-vision
python -m pip install -e .
python -m cascade_vision.cli demo --mock-vlm --event-labels person,backpack --report-json reports/demo.json --events-jsonl reports/events.jsonl
```

演示会生成合成画面，使用简单轮廓检测和预设的模拟描述。依赖安装完成后，不发送 API 请求，也不下载检测模型权重。模拟描述用于展示输出格式，不能证明识别准确率。

### 分析自己的视频

先安装真实检测器，将路径换成有权处理的视频，并使用模拟语义模式检查流程：

```bash
python -m pip install -e ".[yolo]"
python -m cascade_vision.cli run --source local/video.mp4 --mock-vlm --headless --max-frames 300 --event-labels person,backpack --report-json reports/local.json --events-jsonl reports/local-events.jsonl
```

此时检测与跟踪是真实执行的，语义描述仍为模拟结果；YOLO 权重可能在首次使用时下载。摄像头可使用 `--source 0`，视频流则传入 RTSP 地址。

需要真实语义描述时，设置 `OPENAI_API_KEY` 并去掉 `--mock-vlm`。例如：

```bash
python -m cascade_vision.cli run --source local/video.mp4 --max-frames 300 --max-api-calls 10 --event-labels person,backpack --report-json reports/api.json --events-jsonl reports/api-events.jsonl
```

PowerShell 使用 `$env:OPENAI_API_KEY = "your-key"`，Bash 使用 `export OPENAI_API_KEY="your-key"` 设置密钥。真实模式会向服务商发送裁剪图像，并可能产生费用。次数上限包括重试，不等同于美元预算上限。没有 API 预算时使用离线演示即可。

### 对比画面筛选策略

```bash
python examples/generate_fixture.py --output-dir reports/fixture
python -m cascade_vision.cli benchmark --manifest reports/fixture/manifest.json --output-dir reports/comparison --mock-vlm --yolo-model mock
```

这会对同一段带标注的合成视频运行默认策略。真实素材的评测需要准备授权说明和事件标注，并显式允许 API 调用、设置共享次数上限。Windows/Linux 安装步骤、参数配置和 Python 接入方法见 [完整中文指南](docs/GUIDE.zh-CN.md)。

## 结果怎么看

离线演示会在终端打印汇总信息，同时写入两个文件：

| 文件 | 包含什么 | 得到后可以做什么 |
| --- | --- | --- |
| `reports/demo.json` | 运行模式、帧数、任务数量、已观测用量和耗时 | 判断流程是否可靠完成，调整采样频率、队列容量和并发数 |
| `reports/events.jsonl` | 每条任务的状态、时间信息、目标类别、活动描述和已知用量 | 生成待复核清单、按事件标签筛选记录，接入自己的存储或界面 |
| `reports/comparison/benchmark.json` | 运行对比评测后生成的各策略评分与运行指标 | 比较同一段带标注视频上的召回率、误报、延迟和模型用量 |

例如，一条完成的**模拟记录**可以包含下面这些字段。以下为精简后的输出示意：

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

`refined_category` 是描述中的目标类别，`activity_summary` 是活动描述，`observed_events` 是从配置词表中选出的标签。当前默认提示词和模拟描述使用英文，中文文档不会改变模型输出语言。

`simulated: true` 表示预设的模拟结果。真实请求失败或被拒答时，不会被替换成模拟成功结论。`applied: false` 表示该响应过旧或来自上一代连接，未被用于更新当前缓存。

### 这些结果可以用来做什么

- **定位需要复核的片段：** 根据 `media_timestamp_s` 和 `frame_idx`，在原始本地视频中找到对应时刻，结合活动描述和目标框复核画面。你得到的是选中时刻的复核清单，而不是每一帧的完整转录。
- **筛选和整理记录：** 使用 `observed_events`、`refined_category` 和 `activity_summary`，生成按标签组织的待复核列表或类别统计。同一目标可能有多条观察记录，记录条数不能直接当作独立人数或实际事件次数。
- **接入自己的系统：** 读取 JSONL 或消费 Python 回调，把筛选后的数据交给自己的数据库、看板或提醒规则。按目标归组时，应一起保存会话 ID、连接代次和跟踪 ID。
- **选择适合自己的运行策略：** 结合带标注视频的评分与实测用量，选择采样间隔、重试上限和并发配置。调用减少时，也要检查是否出现漏检、失败或队列丢弃。

例如，下面的本地脚本可以读取前面生成的事件文件，打印已完成的人员观察记录，形成待复核清单：

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

示例保留 `simulated` 标记，便于区分演示记录和模型观察结果。模拟记录适合验证接入格式；模型效果与费用分析需要使用真实响应数据。

查看汇总报告时，先检查 `failed_tasks`、`dropped_tasks`、`expired_tasks` 和 `budget_rejected_tasks`，再解读吞吐与筛选效果。`actual_tokens` 使用响应返回的 API 用量：模拟调用为零，完整用量未知时可能为 `null`。成本估算需要自行配置价格，不等于实际账单。

策略对比的主报告位于 `reports/comparison/benchmark.json`。将精确率、召回率和误报与 `quality_flags` 一起看。合成素材和模拟推理会被明确标记，`comparison_ready` 为 `false`；这是验证范围的说明。请求耗时包含排队和模型处理，事件到结论延迟则需要带标注的视频按实时节奏回放。

进一步阅读：[完整中文指南](docs/GUIDE.zh-CN.md)、[事件接口说明](docs/EVENTS.md)、[评测规则](docs/BENCHMARKS.md)、[贡献指南](CONTRIBUTING.md) 和 [更新日志](CHANGELOG.md)。项目源码使用 [MIT 许可证](LICENSE)，第三方模型、软件包和视频素材保留各自的许可证。
