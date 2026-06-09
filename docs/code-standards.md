# Code Standards

> Single source of truth for coding conventions. Both humans and AI agents **must** follow these rules.

---

## 1. Language & Runtime

| Item | Value |
|---|---|
| Python | 3.13+ (no backport code) |
| Type hints | **Required** on all public functions, classes, and module-level variables |
| String quotes | Double quotes (`"`) — enforced by ruff-format |
| Line length | 100 chars max |
| Indent | 4 spaces, no tabs |
| Encoding | UTF-8, LF line endings |

---

## 2. Project Structure

```
membox/
├── src/membox/          # All production code lives here
│   ├── __init__.py
│   ├── py.typed         # PEP 561 marker — do not delete
│   ├── module_a.py
│   └── subpkg/
│       └── __init__.py
├── tests/               # Mirror src/ structure
│   ├── conftest.py      # Shared fixtures
│   ├── test_module_a.py
│   └── subpkg/
│       └── test_subpkg.py
├── docs/                # Project documentation
├── pyproject.toml       # Single config file for everything
└── .github/workflows/   # CI/CD pipelines
```

**Rules:**

- **`src/` layout only** — never import from project root.
- One module per file. Keep files focused (<300 lines ideal, >500 is a smell).
- Test files mirror the source: `src/membox/foo/bar.py` → `tests/foo/test_bar.py`.
- No `utils.py` dumping grounds — split into focused modules (`validators.py`, `converters.py`, etc.).

---

## 3. Naming Conventions

| Element | Style | Example |
|---|---|---|
| Module / package | `snake_case` | `user_profile.py` |
| Class | `PascalCase` | `MemoryStore` |
| Function / method | `snake_case` | `get_user_by_id()` |
| Constant | `UPPER_SNAKE` | `MAX_RETRIES = 3` |
| Private attribute | `_leading_underscore` | `_internal_cache` |
| Type variable | `PascalCase` with `T` prefix or descriptive | `TResult`, `TInput` |
| Pytest fixture | `snake_case` | `db_connection` |
| Pytest test function | `test_<what>_<condition>_<expected>` | `test_parse_empty_input_raises_error` |
| File | `snake_case.py` | `memory_store.py` |

---

## 4. Type Annotations

```python
# ✅ DO: annotate everything public
def fetch_user(user_id: int) -> User | None:
    ...

class MemoryStore:
    def __init__(self, capacity: int) -> None:
        self._data: dict[str, bytes] = {}

# ✅ DO: use modern union syntax (3.13)
def process(data: str | bytes) -> str:
    ...

# ✅ DO: use TYPE_CHECKING block for heavy imports
from __future__ import annotations
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from membox.models import User

# ❌ DON'T: leave public APIs untyped
def process(data):  # mypy will reject this
    ...
```

- Use `from __future__ import annotations` in every file.
- Prefer `type` statement (3.12+) for simple aliases: `type Vector = list[float]`.
- Use `Protocol` over abstract base classes where possible.

---

## 5. Docstrings

Google style, required for **all public** functions/classes:

```python
def calculate_hash(data: bytes, algorithm: str = "sha256") -> str:
    """Calculate a hex digest of the given data.

    Args:
        data: Raw bytes to hash.
        algorithm: Hash algorithm name (default: "sha256").

    Returns:
        Hex-encoded hash string.

    Raises:
        ValueError: If algorithm is not supported.
    """
    ...
```

- One-line summary for simple, self-explanatory functions is acceptable.
- No docstring needed for `__init__` if the class docstring covers it.
- **Never** leave auto-generated stub docstrings like `"TODO: write docs"`.

---

## 6. Error Handling

```python
# ✅ DO: specific exceptions, typed
def load(path: Path) -> Config:
    try:
        raw = path.read_text()
    except FileNotFoundError:
        raise ConfigError(f"Config not found: {path}") from None
    except PermissionError as e:
        raise ConfigError(f"Cannot read {path}: {e}") from e

# ❌ DON'T: bare except or broad Exception
try:
    ...
except Exception:  # too broad
    pass           # silently swallowed
```

**Rules:**

- Define project exceptions in `src/membox/exceptions.py`.
- Always chain with `from` (either `from e` or `from None`).
- Never use bare `except:` or `except Exception: pass`.
- Prefer `raise … from None` when wrapping to hide irrelevant traceback.

---

## 7. Imports

```python
# ✅ Order (enforced by ruff isort):
# 1. stdlib
import os
from pathlib import Path

# 2. third-party
from httpx import Client

# 3. local
from membox.models import User
from membox.exceptions import ConfigError
```

- Use `from __future__ import annotations` as the **first** import in every file.
- No wildcard imports (`from os import *`).
- Use `importlib`-style test imports (already configured in pytest).

---

## 8. Testing

```python
# ✅ DO: descriptive test names, AAA pattern
def test_parse_invalid_json_raises_config_error():
    # Arrange
    raw = "{broken"

    # Act & Assert
    with pytest.raises(ConfigError, match="invalid JSON"):
        parse(raw)

# ✅ DO: use fixtures for expensive setup
@pytest.fixture
def store() -> MemoryStore:
    return MemoryStore(capacity=10)

def test_store_returns_none_on_missing_key(store: MemoryStore):
    assert store.get("nonexistent") is None
```

**Rules:**

- Coverage must stay ≥ 80% (CI enforces this).
- Every bug fix **must** include a regression test.
- Use `pytest.mark.slow` for expensive tests; keep unit tests fast.
- No mocked internals — mock at boundaries (I/O, network, time).
- Tests must be independent and order-independent.

---

## 9. Dependencies

| Category | Tool | Command |
|---|---|---|
| Install | uv | `uv sync` |
| Add dep | uv | `uv add <package>` |
| Add dev dep | uv | `uv add --dev <package>` |
| Remove dep | uv | `uv remove <package>` |
| Lock file | uv.lock | **Always commit** |

- Minimize dependencies. Prefer stdlib.
- Pin minimum versions in `pyproject.toml` (`>=`), uv handles resolution.
- Never modify `uv.lock` manually.

---

## 10. Commit Messages

```
<type>(<scope>): <short summary>

<body if needed>
```

**Types:** `feat` · `fix` · `refactor` · `test` · `docs` · `ci` · `chore` · `perf`

**Examples:**

```
feat(store): add TTL-based expiration to MemoryStore
fix(parse): handle empty input without raising
test(store): add regression test for #42
docs: update code standards for type aliases
ci: add Windows to test matrix
```

- Subject line ≤ 72 chars, imperative mood ("add" not "added").
- Body is optional — use it for context the diff doesn't convey.
- Reference issues: `fixes #12` or `refs #12`.

---

## 11. Enforced by Tooling

These are **not optional** — pre-commit and CI block violations:

| Check | Tool | When |
|---|---|---|
| Format | ruff format | commit + CI |
| Lint | ruff check (30+ rule sets) | commit + CI |
| Type check | mypy --strict | commit + CI |
| Tests | pytest + coverage ≥ 80% | CI |
| Large files | > 500 KB blocked | commit |
| Private keys | detected & blocked | commit |
| Branch protection | no direct push to main/develop | commit |
