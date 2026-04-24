# One-shot agent prompt (dolphinia-nautilus-trader)

Paste this as the **entire** user message when you want the agent to work autonomously without repeated “proceed” nudges.

---

You are in repo **`dolphinia-nautilus-trader`** (Dolphinia fork of NautilusTrader). Work **end-to-end**: investigate, change code only when needed, run checks, and **report once** when finished.

## Goals (in order)

1. **Git**: Keep `develop` clean; do not drop Dolphinia-only commits (`build_win_mt5/`, etc.) without explicit direction.
2. **Upstream**: Prefer **`upstream/develop`** merged regularly; resolve conflicts in favor of upstream unless a file is clearly Dolphinia-specific.
3. **Windows dev loop**: Native extensions often are not built locally. Use:
   - `NAUTILUS_TEST_SITE_PACKAGES=1` when running pytest from a checkout that shadows wheels.
   - A **matching** binary: latest **`1.x.xaYYYYMMDD`** wheel from `https://packages.nautechsystems.io/simple` (or PyPI release), aligned roughly with `pyproject.toml` version. If wheels lag new Python/Cython APIs, `tests/conftest.py` may **skip** a small set of tests until binaries catch up or you build from source.
4. **Tests**: From repo root, Python 3.12+:
   ```powershell
   $env:NAUTILUS_TEST_SITE_PACKAGES = "1"
   py -3.12 -m pip install -U "nautilus_trader" --pre --index-url "https://packages.nautechsystems.io/simple" --extra-index-url "https://pypi.org/simple"
   py -3.12 -m pytest tests/unit_tests -q
   ```
   Install optional test deps as collection errors demand (e.g. `betfair-parser`, `plotly`, `aiohttp` per `pyproject.toml`).
5. **Source build (Windows)**: Requires Rust, LLVM/Clang, CMake, MSVC. If link errors persist, document and rely on wheels/CI; do not rip out upstream build logic without an issue link.
6. **Deliverable**: Single final summary: what changed, commands run, pass/fail counts, and any remaining risks.

## Constraints

- Prefer **small, focused diffs**; match upstream style.
- Do **not** ask the user to re-run steps you can run yourself.

---

_End of prompt._
