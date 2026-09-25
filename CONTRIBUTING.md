# Contributing

Use Python 3.10 or newer and work in a repository-local virtual environment.

```bash
python -m venv .venv
.venv/bin/python -m pip install -e .
.venv/bin/python -m unittest discover -s tests -v
```

Capability names are public contracts. A backend may advertise a capability
only after an automated test exercises it. A discovered executable is not an
implemented adapter. New lossy geometry operations must report before/after
measurements and preserve source files.

Unit tests must not contact a printer, start a print, or depend on proprietary
models. Put slow tests that invoke optional executables in an explicit E2E
script and document the required environment variable.

