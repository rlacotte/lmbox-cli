---
title: Release process
description: How to ship a new version of lmbox-cli to PyPI.
---

# Release process

## One-time setup (already done? skip)

These steps must be done **once**, by a repo admin with PyPI access.
Once they're in place, every subsequent release is one command.

### 1 · Register the project on TestPyPI

1. Create an account on <https://test.pypi.org> if you don't have one.
2. Go to <https://test.pypi.org/manage/account/publishing/> →
   **Add a new pending publisher**.
3. Fill in:
   - **PyPI Project Name**: `lmbox-cli`
   - **Owner**: `rlacotte`
   - **Repository name**: `lmbox-cli`
   - **Workflow name**: `publish.yml`
   - **Environment name**: `testpypi`
4. Save.

### 2 · Same on PyPI proper

1. Create an account on <https://pypi.org> if you don't have one.
2. Go to <https://pypi.org/manage/account/publishing/> → **Add a new pending publisher**.
3. Same fields as above, but **Environment name**: `pypi`.
4. Save.

### 3 · Create the GitHub environments

On <https://github.com/rlacotte/lmbox-cli/settings/environments>:

1. Click **New environment** → name it `testpypi` → Save (no secrets needed,
   OIDC handles auth via Trusted Publishing).
2. Repeat with `pypi`.

Optionally protect the `pypi` environment with a manual approval step
so a human reviews each non-prerelease before it goes live.

## Per-release workflow

Releases are driven by **git tags matching `v*`**. Workflow:

```bash
# 1. Bump version in pyproject.toml + lmbox_cli/__init__.py
$EDITOR pyproject.toml          # version = "0.2.0"
$EDITOR lmbox_cli/__init__.py   # __version__ = "0.2.0"

# 2. Commit + tag
git commit -am "Release v0.2.0"
git tag v0.2.0
git push origin main --tags
```

The `publish.yml` workflow then:

1. Runs `pytest` (release blocker on failure).
2. Builds `wheel + sdist` with `python -m build`.
3. Verifies templates landed in the wheel (sanity).
4. Uploads to **TestPyPI** — verify with:
   `pip install -i https://test.pypi.org/simple/ lmbox-cli`
5. If the tag is **not** a prerelease (no `a`, `b`, `rc` in the tag name),
   also uploads to **PyPI**.

## Prerelease conventions

| Tag pattern | TestPyPI | PyPI |
|---|---|---|
| `v0.2.0a1`, `v0.2.0b2`, `v0.2.0rc1` | ✓ | ✗ (skipped) |
| `v0.2.0` | ✓ | ✓ |

Use prereleases to validate the artefact against a real `pip install`
before exposing partners to it.

## After the first stable release

Add a PyPI badge to the README (it auto-updates):

```markdown
[![PyPI](https://img.shields.io/pypi/v/lmbox-cli.svg)](https://pypi.org/project/lmbox-cli/)
```

It's already there (rendering as "no releases" until 0.1.0 lands).
