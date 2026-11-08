# cascade-vision

通过 OpenCV 运动门控、YOLO + ByteTrack 跟踪和后台 VLM 工作线程，筛选需要语义分析的视频区域。

**当前为早期 alpha。** 已提供离线演示与回归测试，尚未验证真实视频的事件召回率、准确率和成本节省比例。完整接口说明见 [English README](README.md)。

```bash
git clone https://github.com/x549d/cascade-vision.git
cd cascade-vision
python -m pip install -e ".[dev]"
cascade-vision demo --mock-vlm --report-json reports/demo.json
```

离线演示使用固定种子的合成视频、颜色轮廓检测器和预设模拟结论，不发送 API 请求。真实 YOLO 需要安装 `.[yolo]`；真实 VLM 需要设置 `OPENAI_API_KEY`，会发送裁剪图像并产生费用。

- 缺少密钥时明确报错；只有显式模拟模式才生成模拟结论。
- YOLO 加载失败、空检测、API 失败和模型拒答不会变成模拟成功结果。
- 实际 token 使用响应 `usage` 统计；未知时报告 `null`，保留已观测的部分用量。
- 图像估算与实际用量分开；价格由用户配置，成本估算不等同于账单。
- 单独记录失败、丢弃、取消与未完成任务，限制缓存，防止旧异步结果覆盖新结论。
- 跳过原始帧不会中断连续检测观测计数；检测器明确未发现目标时重置计数。

见 [贡献指南](CONTRIBUTING.md) 和 [评测方法](docs/BENCHMARKS.md)。暂不提供未经验证的生产级可靠性或节省比例承诺。
