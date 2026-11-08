# cascade-vision — Cascaded Video Understanding / 级联视频语义分析

**通过本地运动筛选、目标检测与跟踪，选择需要 VLM 分析的画面区域，输出结构化语义事件，并评测不同策略的调用量、召回率和延迟。**

[English / 首页](README.md) · [完整中文指南](docs/GUIDE.zh-CN.md) · [版本下载](https://github.com/x549d/cascade-vision/releases) · [事件接口](docs/EVENTS.md)

## Purpose / 项目作用

`cascade-vision` 是一个开源 Python 库与命令行工具，适合开发视频语义分析原型、接入事件结果，以及研究画面筛选与模型调用之间的权衡。

处理流程为：**视频输入 → OpenCV 运动门控 → YOLO + ByteTrack 检测跟踪 → 触发与裁剪 → 异步 VLM 分析 → 结构化结论与事件文件**。

- 筛选画面：结合运动变化和定期检测，决定何时运行目标检测器。
- 选择目标：对优先目标、低置信度或重叠目标触发语义审查。
- 返回事件：导出类别、活动摘要、事件标签、任务状态、用量和耗时。
- 恢复故障：支持有限重试、结果刷新、队列过期及 RTSP 重连。
- 比较策略：使用带标注视频比较固定间隔、运动筛选、级联和逐帧策略。

**当前为早期 alpha。** 工具和离线回归测试已经可用，真实视频准确率、成本节省及独立采用效果仍待验证。

## Quick Start / 快速开始

需要 Python 3.9 或以上版本。在终端中执行：

```bash
git clone https://github.com/x549d/cascade-vision.git
cd cascade-vision
python -m pip install -e .
python -m cascade_vision.cli demo --mock-vlm --report-json reports/demo.json --events-jsonl reports/events.jsonl
```

演示自行生成合成画面。依赖安装完成后，不需要本地视频、摄像头、模型权重或 API 密钥，不产生 VLM API 费用。模拟语义结果来自预设规则。

| 输出 | 作用 |
| --- | --- |
| `reports/demo.json` | 查看版本、运行模式及汇总指标 |
| `reports/events.jsonl` | 查看逐条任务状态、语义结论、用量与耗时 |

## Examples / 使用示例

### Offline Comparison / 离线策略对比

```bash
python examples/generate_fixture.py --output-dir reports/fixture
python -m cascade_vision.cli benchmark --manifest reports/fixture/manifest.json --output-dir reports/comparison --mock-vlm --yolo-model mock
```

生成配套视频与标注，比较三种默认策略。主报告位于 `reports/comparison/benchmark.json`，各策略另有 JSONL 事件文件。合成素材和模拟模式会被明确标注。

### Local Video / 本地视频

```bash
python -m pip install -e ".[yolo]"
python -m cascade_vision.cli run --source local/video.mp4 --mock-vlm --headless --max-frames 300 --event-labels person,backpack --report-json reports/local.json --events-jsonl reports/local-events.jsonl
```

将路径替换为有权使用的视频。此模式运行真实 YOLO 检测与跟踪，但 VLM 仍是模拟，不发送 VLM API 请求；YOLO 权重可能在首次使用时下载。

### Real VLM / 真实语义分析

具备 API 预算时可设置 `OPENAI_API_KEY`，去掉 `--mock-vlm`：

```bash
python -m cascade_vision.cli run --source local/video.mp4 --max-frames 300 --max-api-calls 10 --event-labels person,backpack --report-json reports/api.json --events-jsonl reports/api-events.jsonl
```

PowerShell 使用 `$env:OPENAI_API_KEY = "your-key"`；Bash 使用 `export OPENAI_API_KEY="your-key"`。真实模式会发送裁剪图像并可能产生费用；次数上限包括重试，不是美元预算上限。

### Python API / Python 接入

```python
from cascade_vision import CascadeConfig, CascadePipeline
from cascade_vision.cli import generate_synthetic_stream

config = CascadeConfig.from_env(mock_vlm=True, yolo_model="mock")
pipeline = CascadePipeline(config)
report = pipeline.run_stream(generate_synthetic_stream(), source_fps=30)
print(report.to_dict())
```

## Configuration and Evaluation / 配置与评测

使用 `CASCADE_<字段名大写>` 环境变量或 `CascadeConfig.from_env()` 配置参数。常见项包括队列容量、工作线程数、失败重试、结果有效期、静态画面检测间隔和总尝试次数限制。

评测结果应同时观察事件召回率、误报、调用用量和端到端延迟，并检查失败、丢弃、过期和额度耗尽标记。合成演示不能证明真实场景效果。

完整的 Windows/Linux 安装命令、配置表、事件状态、评分规则和故障排查见 [中文使用与评测指南](docs/GUIDE.zh-CN.md)。

## Documentation / 更多文档

- [评测流程](docs/BENCHMARKS.md)：视频清单、策略对比与结果有效性。
- [事件接口](docs/EVENTS.md)：JSONL 格式、回调接入和结果有效期。
- [试用指南](docs/PILOT_GUIDE.md)：独立试用流程与反馈方式。
- [贡献指南](CONTRIBUTING.md)：开发、测试和贡献约定。
- [更新日志](CHANGELOG.md)：版本变化与迁移说明。

## License / 许可证

项目源码使用 [MIT 许可证](LICENSE)。第三方软件、模型权重与素材保留各自的许可证和使用条件。
