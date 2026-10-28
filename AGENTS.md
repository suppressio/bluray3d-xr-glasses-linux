# Instructions for coding agents

For AI coding agents (Claude Code, GitHub Copilot and the like) working on this repository. People should start from the [README](README.md) and [docs/DEVELOPMENT.md](docs/DEVELOPMENT.md); this file only adds what an agent needs on top of them.

## Read first

- [docs/DEVELOPMENT.md](docs/DEVELOPMENT.md): where things are in the code, the checks, the tests, the logs.
- [docs/OPTIONS.md](docs/OPTIONS.md): every command-line option, when your change adds or changes one.
- [docs/RESEARCH.md](docs/RESEARCH.md): Blu-ray playlists (MPLS), play-all playlists, title selection.

## Branches and pull requests

- `next` is the default branch, `main` is production. Never commit or push to either.
- Work on a branch from `next` named `<type>/<issue>-<slug>`, e.g. `fix/23-read-error-message`. Types: `feat`, `fix`, `docs`, `chore`, `refactor`, `test`, `ci`.
- One pull request per issue, to `next`. Its description starts with `Refs #<issue>` (not `Closes`: the issue is closed after the test on real hardware), then a summary and a test plan with what you ran and what is left to a person.
- Commit messages: short, lowercase type prefix as in the history (`fix: …`, `docs: …`, `ci: …`).

## Checks

```bash
scripts/check.sh          # ruff, pyright (strict), shellcheck, unit tests: must pass
```

- The program's own dependencies come from the system: `sudo apt-get install -y python3-pyfuse3 python3-trio python3-venv ffmpeg`. `check.sh` creates `.venv` with the pinned tools.
- Every fix comes with the unit test that would have caught it. Unit tests need no disc: use `tests/builders.py` (real MPLS/CLPI/IFO/TS files) and `tests/fakes.py` (`FakeBluray`, `FakeDvd`).
- **No drive in the cloud.** The integration tests (`scripts/check.sh --integration`) need a disc in a real drive. If you cannot run them, say so in the test plan and list what a person must check on real hardware: do not mark them as done.

## Conventions

- Code, comments and log messages in English.
- Documentation in two languages: every `.md` file has an `.it.md` twin, changed in the same pull request. Exceptions: this file and `CLAUDE.md`, which are for agents only. The README is for users (what it is, install, use); every other document goes in `docs/`, one file per topic.
- Keep the style of the surrounding code; pyright strict means typed signatures everywhere.
- Do not change `.github/workflows/` unless the issue asks for it.
