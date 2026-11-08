# Contributing

Issues and pull requests are welcome. For substantial changes, open an issue describing the behavior, use case and proposed approach.

```bash
python -m pip install -e ".[dev]"
python -m pytest -q
python -m ruff check cascade_vision tests examples
python -m ruff format --check cascade_vision tests examples
python -m build
```

Tests must run offline without credentials, cameras or downloaded weights. Inject provider responses for success, refusal and failure tests. Keep simulation and measured usage distinct.

Bug reports should include Python/package versions, a minimal reproduction and sanitized traceback. Do not include API keys, credential-bearing RTSP URLs, base64 images or private footage. Follow [the evaluation protocol](docs/BENCHMARKS.md) for performance claims.

## Roadmap

- Collect authorized real-video annotations and publish independently reproducible measurements.
- Recruit independent pilots using docs/PILOT_GUIDE.md and the pilot feedback issue template.
- Validate comparison quality on real workloads and iterate on negative results.
- Add multi-frame temporal context for interactions and stationary objects.
- Opt-in provider integration tests with budget limits.

Contributions use the project's MIT license. Identify third-party code, weights or data and preserve their license requirements.
