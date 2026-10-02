# Releasing to PyPI

`.github/workflows/release.yml` publishes `form-extract` with PyPI Trusted Publishing, so no
API token is stored anywhere. A PyPI release is permanent and public, and a version number can
never be reused.

**Before the first release (yours to settle, not the code's):** confirm ownership of the work
with your employer in writing, and publish from a personal PyPI account. Note that the PDF
ingester depends on PyMuPDF, which is AGPL-3.0 (or commercial from Artifex); `NOTICE` and the
README say so.

## One-time setup

1. Accounts on pypi.org and test.pypi.org with 2FA.
2. On each site add a *pending publisher* for `form-extract`: owner `iortega10`, repository
   `form-extract`, workflow `release.yml`, environment `pypi` (PyPI) or `testpypi` (TestPyPI).
3. In the GitHub repository create the environments `pypi` and `testpypi`.
4. Make the repository public when ready (the project links point at it).

## Each release

1. Set `version` in `pyproject.toml` to the exact version you will tag (`0.1.0rc1` for the dry
   run, `0.1.0` for the real one); the workflow refuses a tag that does not match.
2. Dry run: tag `v0.1.0rc1` -> TestPyPI. In a clean virtualenv:
   `pip install --index-url https://test.pypi.org/simple/ --extra-index-url https://pypi.org/simple/ form-extract`
   and run `formextract-eval`.
3. Real release: set `0.1.0`, tag `v0.1.0` -> PyPI. Verify with a fresh `pip install form-extract`.
