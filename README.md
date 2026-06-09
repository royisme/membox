# membox

> membox

## Quick start

```bash
# Install uv (if not already)
curl -LsSf https://astral.sh/uv/install.sh | sh

# Create venv & install dev deps
uv sync

# Run tests
uv run pytest

# Lint & format
uv run ruff check .
uv run ruff format .

# Type check
uv run mypy src
```

## Project structure

```
membox/
├── src/membox/        # Source package
├── tests/             # Test suite
├── .github/           # CI/CD
├── pyproject.toml     # Project config (uv, ruff, pytest, mypy)
└── README.md
```

## License

MIT
