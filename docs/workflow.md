# Development Workflow

> How to work in this repo — for humans and AI agents.

---

## Branching Model

```
main        ← stable, release-ready. Only receives PRs.
              Protected: no direct commits.
develop     ← day-to-day integration branch.
              Protected: no direct commits.
feature/*   ← created from develop, PR back to develop.
fix/*       ← created from develop, PR back to develop.
release/*   ← created from develop, PR to main for release.
```

### Rules

1. **Never commit directly to `main` or `develop`** — pre-commit blocks this.
2. All work happens on feature/fix branches.
3. All merges go through PRs with passing CI.

---

## Daily Workflow

### Start a new task

```bash
git checkout develop
git pull origin develop
git checkout -b feature/my-task
```

### During development

```bash
# Install / update dependencies
uv sync

# Run checks locally (pre-commit runs automatically on commit)
uv run pytest                    # tests
uv run ruff check .              # lint
uv run ruff format .             # format
uv run mypy src                  # type check

# Or run everything at once
uv run pre-commit run --all-files
```

### Commit your work

```bash
git add -A
git commit -m "feat(scope): description of change"
# pre-commit hooks run automatically: ruff, mypy, branch protection
```

If pre-commit fixes files (e.g. ruff auto-format), review the changes and commit again:

```bash
git diff                         # review auto-fixes
git add -A
git commit -m "feat(scope): description of change"
```

### Push and create PR

```bash
git push -u origin feature/my-task

# Then create PR on GitHub:
#   base: develop  ←  compare: feature/my-task
```

### Merge to main (release)

When `develop` is stable and ready to ship:

```bash
# Create PR on GitHub:
#   base: main  ←  compare: develop
# All CI checks must pass before merge.
```

---

## CI Pipeline

Every push to `develop`/`main` and every PR targeting `main` triggers:

```
┌──────────┐  ┌──────────┐  ┌──────────┐  ┌──────────────────┐
│  FORMAT  │  │   LINT   │  │ TYPECHECK│  │      TEST        │
│  (ruff)  │  │  (ruff)  │  │  (mypy)  │  │ (pytest+coverage)│
│          │  │          │  │          │  │ 3-OS matrix      │
└────┬─────┘  └────┬─────┘  └────┬─────┘  └───────┬──────────┘
     │             │             │                 │
     └─────────────┴─────────────┴─────────────────┘
                           │
                    ┌──────▼──────┐
                    │  CI GATE ✅ │
                    │  all pass   │
                    └─────────────┘
```

| Job | What it checks | Blocking |
|---|---|---|
| **Format** | `ruff format --check --diff` | ✅ |
| **Lint** | `ruff check` with GitHub annotations | ✅ |
| **Type check** | `mypy --strict` | ✅ |
| **Test** | `pytest` + coverage ≥ 80%, Ubuntu/macOS/Windows | ✅ |
| **CI Gate** | All 4 must pass → merge allowed | ✅ |

---

## PR Rules

1. **All CI checks must pass** before merge.
2. **Squash merge** by default (keeps `main` and `develop` clean).
3. PR title = conventional commit format (same as commit messages).
4. Delete branch after merge.
5. At least 1 approval required for PRs to `main`.

---

## Release Process

```bash
# 1. Create a release branch from develop
git checkout develop
git pull origin develop
git checkout -b release/x.y.z

# 2. Run the full local verification gate
uv run pytest
uv run pre-commit run --all-files

# 3. Bump version metadata in pyproject.toml and src/membox/__init__.py
uv run python scripts/bump_version.py x.y.z

# 4. Generate the release changelog section from git history
uv run python scripts/generate_changelog.py --version x.y.z

# 5. Review generated changes, then commit on the release branch
git diff
git add pyproject.toml src/membox/__init__.py CHANGELOG.md docs/repository-map.md
git commit -m "chore(release): prepare x.y.z"

# 6. Push and create PR to main
git push -u origin release/x.y.z
# Create PR: main ← release/x.y.z

# 7. After merge, tag on main
git checkout main
git pull origin main
git tag -a vx.y.z -m "Release x.y.z"
git push origin vx.y.z
```

### Release Automation

- `scripts/bump_version.py x.y.z` is the only supported way to update release version metadata.
- `scripts/bump_version.py x.y.z --check` verifies both version fields already match.
- `scripts/generate_changelog.py --version x.y.z` inserts or replaces the target release section in `CHANGELOG.md`.
- `scripts/generate_changelog.py --check` verifies the current `Unreleased` section is in sync with git history.
- Both scripts are stdlib-only and safe to run without network access.

---

## For AI Agents

When working as a coding agent in this repo, follow these rules:

1. **Read this file first** at session start.
2. **Read `docs/code-standards.md`** before writing any code.
3. Always work on a **feature branch** — never on `main` or `develop`.
4. Run `uv run pytest` after every change. Fix failures before proceeding.
5. Run `uv run ruff check . && uv run ruff format .` before committing.
6. Run `uv run mypy src` before committing — zero errors required.
7. All new public APIs **must** have type hints and docstrings.
8. All new code **must** include tests (coverage ≥ 80%).
9. Commit messages follow conventional commits format (see code-standards.md).
10. If a pre-commit hook fails, fix the issue and re-commit. Do not `--no-verify`.

### Quick reference

```bash
uv sync                              # install everything
uv run pytest -x                     # run tests, stop on first failure
uv run pytest --cov                  # run with coverage report
uv run ruff check --fix .            # lint with auto-fix
uv run ruff format .                 # format code
uv run mypy src                      # type check
uv run pre-commit run --all-files    # run all checks
```
