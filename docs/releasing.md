# Releasing

Two channels: PyPI for `pipx install machop`, Homebrew for `brew install machop`.
Both key off a `vX.Y.Z` git tag, so cut the tag first and everything else
follows from it.

## 1. Tag the release

`version` in `pyproject.toml` and the tag must agree, or the Homebrew formula
will point at a tarball whose metadata says something else.

```bash
# bump `version` in pyproject.toml first
git commit -am "Release 0.2.0"
git tag -a v0.2.0 -m "0.2.0"
git push origin main --tags
```

Then **Releases → Draft a new release** on GitHub, pick the tag, and
**Generate release notes**.

## 2. PyPI

One-time: create an account with 2FA, then an API token at
[pypi.org/manage/account/token](https://pypi.org/manage/account/token). Put it
in `~/.pypirc`:

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

Do the first one against TestPyPI. **A version number can never be reused on
PyPI**, even after deleting it, so a bad upload costs you the number:

```bash
python -m twine upload --repository testpypi dist/*
```

## 3. Homebrew

Homebrew's core repository only accepts packages with a substantial user base,
so the route for a new tool is your own **tap** - a repository named
`homebrew-<something>` that Homebrew knows how to read.

### Create the tap, once

Make a GitHub repository called **`homebrew-tap`**. Then:

```bash
git clone https://github.com/jainsiddharth99/homebrew-tap.git
cd homebrew-tap
mkdir -p Formula
```

### Write the formula

Get the checksum of the PyPI tarball for the version you are releasing:

```bash
curl -sL https://files.pythonhosted.org/.../machop-0.2.0.tar.gz | shasum -a 256
```

(the exact URL is on the release's page at pypi.org/project/machop/#files)

Create `Formula/machop.rb`:

```ruby
class Machop < Formula
  include Language::Python::Virtualenv

  desc "Stream a Mac's screen to any browser and control it remotely"
  homepage "https://github.com/jainsiddharth99/machop"
  url "https://files.pythonhosted.org/packages/.../machop-0.2.0.tar.gz"
  sha256 "PUT_THE_CHECKSUM_HERE"
  license "MIT"

  depends_on :macos
  depends_on "python@3.12"

  def install
    virtualenv_install_with_resources
  end

  test do
    assert_match "machop", shell_output("#{bin}/machop --version")
  end
end
```

The dependencies need to be listed as `resource` blocks. Generate them rather
than writing them by hand:

```bash
brew install homebrew/cask/...  # nothing needed; the tool below ships with brew
pip install homebrew-pypi-poet
poet machop >> Formula/machop.rb   # paste the resource blocks into the formula
```

Then:

```bash
git add Formula/machop.rb
git commit -m "machop 0.2.0"
git push
```

### What people run

```bash
brew tap jainsiddharth99/tap
brew install machop
```

or in one line:

```bash
brew install jainsiddharth99/tap/machop
```

### Updating

For each release: bump `url` and `sha256` in the formula, regenerate the
resources if dependencies changed, commit, push. Anyone on
`brew upgrade machop` picks it up.

## Notes

- Machop needs **Python 3.10+**. The formula pins `python@3.12` so it does not
  break when Homebrew's default python moves ahead of what `av` and `aiortc`
  publish wheels for - which is a real problem: there are no 3.14 wheels yet.
- `depends_on :macos` is not decoration. The tool captures a Mac's screen and
  exits with a message anywhere else.
