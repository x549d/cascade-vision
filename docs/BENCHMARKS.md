# Evaluation methodology

## Offline smoke test

```bash
python -m pip install -e ".[dev]"
cascade-vision demo --mock-vlm --report-json reports/demo.json
```

The generator uses seed 42 and 300 frames at 640x480: 100 static, 150 with colored moving targets and noise, then 50 static. The detector is a contour heuristic and semantic outputs are mock profiles. Timing varies by machine. This validates execution and reporting only; it cannot establish real YOLO/VLM accuracy, event recall or API savings.

JSON records package version, Python, platform, model, detail, simulation modes and metrics. Credentials, source URLs and image payloads are omitted. Generated reports are ignored by Git. Review them before sharing.

## Real-video protocol

No real-video results are published yet. To contribute evidence:

1. Identify redistributable footage or its original download link and license. Record hashes, dimensions, duration, source FPS and event annotations. Do not redistribute private footage.
2. Record source commit, configuration without secrets, exact dependency versions, CPU/GPU and provider model snapshot.
3. Compare full-frame sampling, fixed-interval sampling (such as 1 FPS), motion-only sampling and the complete cascade. Match model, detail, semantic prompt and event definitions, disclosing differences.
4. Save request usage, errors, drops, timing and predictions. Include text input and output tokens in cost. Record actual provider rates and measurement date.
5. Measure event precision/recall, false alarms per hour, event-to-verdict latency including queue wait (median and p95), throughput, API requests and billed usage. Include static-scene and camera-motion failures.
6. Publish scripts, sanitized raw logs and annotations so others can repeat the experiment. Repeat runs and disclose variation.

The current CLI reports aggregate service latency, not end-to-end event latency or annotations. Real evaluation needs an additional event annotation/evaluation harness, tracked in the roadmap. Do not substitute frame filtering or service latency for event recall.
