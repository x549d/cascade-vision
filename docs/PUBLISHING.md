# Publishing cascade-vision

The GitHub release workflow checks that the version matches the tag, runs the tests,
builds a wheel and source distribution, and attaches both distributions to the release.
Python uses `0.1.0a0` for the Git tag `v0.1.0-alpha`. The GitHub release is marked as a
prerelease so it does not replace the latest stable release.

## First PyPI publication

In [PyPI publishing settings](https://pypi.org/manage/account/publishing/), add a
pending GitHub Trusted Publisher with these values.

| Field | Value |
| --- | --- |
| PyPI project name | `cascade-vision` |
| Repository owner | `x549d` |
| Repository name | `cascade-vision` |
| Workflow filename | `publish-pypi.yml` |
| Environment name | `pypi` |

From **Actions → Publish to PyPI → Run workflow**, run the workflow on `main`
and select the existing `v0.3.0` release tag first. With a stable version
published, pip can install the package using its default version selection.

```bash
python -m pip install cascade-vision
cascade-vision --help
```

To publish the alpha too, run the workflow again with `v0.1.0-alpha` once that
GitHub prerelease exists. Install that version explicitly with
`python -m pip install cascade-vision==0.1.0a0`.
