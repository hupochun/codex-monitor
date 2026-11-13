"""Build local distributable artifacts; never publish or access a package index."""
import hashlib
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from codex_monitor import __version__
from codex_monitor.distribution import build_skill_archive


def main():
    destination = ROOT / 'dist'
    destination.mkdir(exist_ok=True)
    subprocess.run([sys.executable, '-m', 'pip', 'wheel', '--no-deps',
                    '--no-build-isolation', '--no-index', str(ROOT),
                    '--wheel-dir', str(destination)], check=True, cwd=ROOT)
    wheel = destination / f'codex_training_monitor-{__version__}-py3-none-any.whl'
    if not wheel.is_file():
        raise SystemExit('Expected wheel was not produced')
    skill = build_skill_archive(destination / f'codex-training-monitor-{__version__}-skill.zip')
    checksums = '\n'.join(hashlib.sha256(path.read_bytes()).hexdigest() + '  ' + path.name
                          for path in (skill, wheel)) + '\n'
    (destination / 'SHA256SUMS').write_text(checksums, encoding='utf-8')
    print(checksums, end='')


if __name__ == '__main__':
    main()
