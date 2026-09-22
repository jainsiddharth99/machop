# Releasing

One channel: PyPI, so people install with `pipx install machop`.

## 1. Agree the version

`version` in `pyproject.toml`, the git tag, and the commit message must all
say the same thing. They currently do not - the first commit is called
"Machop 0.1.0" while `pyproject.toml` says `0.2.0`.

```bash
# in pyproject.toml, set:  version = "0.2.0"
grep '^version' pyproject.toml
```

## 2. Commit and tag

```bash
git add -A
git commit -m "Release 0.2.0"
git tag -a v0.2.0 -m "0.2.0"
git push origin main --tags
```

Then on GitHub: **Releases → Draft a new release → pick `v0.2.0` → Generate
release notes**.

## 3. Publish to PyPI

One-time: an account with 2FA, then an API token at
[pypi.org/manage/account/token](https://pypi.org/manage/account/token), saved
to `~/.pypirc`:

```ini
[pypi]
  username = __token__
  password = pypi-YOUR-TOKEN
```

Every release:

```bash
rm -rf dist build
python -m build
python -m twine check dist/*
python -m twine upload dist/*
```

**A version number can never be reused on PyPI**, even after deleting it. A
bad upload costs you that number permanently, so do the first one against
TestPyPI:

```bash
python -m twine upload --repository testpypi dist/*
pipx install --index-url https://test.pypi.org/simple/ --pip-args="--extra-index-url https://pypi.org/simple/" machop
```

## 4. Check it from a clean machine's point of view

The useful test is not "does it import" but "does a fresh environment end up
with something that runs":

```bash
python3 -m venv /tmp/check && /tmp/check/bin/pip install -U pip
/tmp/check/bin/pip install machop
/tmp/check/bin/machop --no-tunnel --pin 424242 --port 19099 &
sleep 8 && curl -s http://127.0.0.1:19099/health   # {"ok": true, ...}
```

The thing this catches is the viewer's assets going missing from the wheel.
`src/machop/web/*` is shipped by `[tool.setuptools.package-data]`; lose that
and the tool starts perfectly and serves a blank page.

## Notes on other machines

- **Python 3.10 to 3.14** all work. Verified on a clean 3.14 environment:
  every dependency resolved to a wheel and the tool ran.
- **macOS 13+**, for ScreenCaptureKit. On anything else the tool prints one
  line and exits 1 rather than raising.
- **Screen Recording and Accessibility** have to be granted. macOS asks the
  first time, and the grant is per-binary - so a person who moves from `pipx`
  to a venv will be asked again. Machop checks both at startup and names
  whichever is missing.
- **No tunnel binary is required.** Without `cloudflared` the default falls
  back to `localhost.run`, which only needs `ssh`. Measured: a live, serving
  URL in 2.9 s.
