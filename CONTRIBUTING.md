# Contributing

Issues and pull requests are welcome. For substantial changes, open an issue describing the behavior, use case and proposed approach.

```bash
python -m pip install -e ".[dev]"
python -m pytest -q
python -m ruff check cascade_vision tests
python -m ruff format --check cascade_vision tests
python -m build
```

Tests must run offline without credentials, cameras or downloaded weights. Inject provider responses for success, refusal and failure tests. Keep simulation and measured usage distinct.

Bug reports should include Python/package versions, a minimal reproduction and sanitized traceback. Do not include API keys, credential-bearing RTSP URLs, base64 images or private footage. Follow [the evaluation protocol](docs/BENCHMARKS.md) for performance claims.

## Roadmap

- Licensed real-video fixtures and event annotations.
- Fixed-interval and motion-only comparison runners.
- Event precision/recall and end-to-end latency evaluation.
- Track refresh policies and temporal context for stationary objects.
- Opt-in provider integration tests with budget limits.

Contributions use the project's MIT license. Identify third-party code, weights or data and preserve their license requirements.
