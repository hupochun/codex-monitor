# Validation

## Version 0.2.0

Validation host: Windows, Python 3.14.6. Date: 2026-11-13.

- **66 automated tests passed** with `python -m unittest discover -s tests -q`.
- `python -m compileall -q codex_monitor scripts examples` passed.
- A relocated Skill ZIP ran from a directory containing spaces without the
  original checkout or PYTHONPATH, installed another copy, and completed detached
  mock training with delivery disabled.
- The wheel installed offline in a fresh virtual environment. Its console entry,
  embedded Skill installer and detached mock training all passed.
- Worker startup and acknowledgement portability regressions failed before the
  change and passed after it. Acknowledgements work from an unrelated cwd.
- Installer tests cover idempotency, upgrade backup, user modifications, legacy
  path-pointer migration, concurrent installation and symbolic-link/reparse checks.

## Desktop continuation evidence

A prior integration run on the same Windows desktop App verified both active-chat
message delivery and idle-chat wake-up:

1. A detached 45-step training stand-in reached healthy status.
2. The assistant ended its turn.
3. The job completed successfully; its native App message started a new turn in
   the same original chat 33 seconds after the preceding turn ended.
4. The resumed chat read the result and acknowledged the matching event. Delivery
   transitioned to confirmed. There was no model polling in the idle interval.

The packaging change retested isolated worker startup and acknowledgement without
sending further live messages. Conversation identifiers and machine-local paths
are intentionally omitted from this public record.

## Coverage and limitations

- Local tests exercise large stdout, finite increasing training progress,
  startup/stall/runtime deadlines, Windows descendant cleanup, SQLite claims,
  delivery uncertainty and acknowledgement races.
- SSH tests use mocked transport and temporary protocol execution. They cover
  idempotent launch retry, log draining, UTF-8 boundaries and process-group cleanup.
  No actual SSH Linux host or GPU training was used for this release.
- Python 3.11+ is the supported target, but this release was tested on Python 3.14.
  Linux, WSL, Slurm and cloud-platform end-to-end runs are not verified.
- Native messaging uses the installed desktop App's local plugin protocol, not a
  guaranteed stable public API. App updates or restart may invalidate the pipe.
- Machine sleep/reboot and worker hard crashes are not transparently recoverable.
  Persistent records remain; training is never blindly rerun.
- Submission and acknowledgement are separate. Unknown sends are not replayed;
  exactly-once processing across all crash windows is not claimed.
- The bundled skill validator required unavailable PyYAML; no runtime dependency
  was added. Frontmatter, entry points and installed skill behavior were checked
  using the standard library and executable acceptance tests.

## Reproduce

```powershell
python -m unittest discover -s tests -q
python -m compileall -q codex_monitor scripts examples
python scripts/build_release.py
python scripts/verify_wheel.py dist/codex_training_monitor-0.2.0-py3-none-any.whl
```

Build dependencies are pip and setuptools>=68; the installed monitor itself has no
third-party runtime dependencies. Build artifacts include a Skill ZIP, wheel and
SHA256SUMS. Tests do not send real Codex messages.
