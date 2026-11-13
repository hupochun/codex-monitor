"""Build and install a complete skill without developer-checkout pointers."""
import hashlib
from contextlib import contextmanager
import json
import os
from pathlib import Path
import shutil
import stat
import tempfile
import uuid
import zipfile

from . import __version__

NAME = 'codex-training-monitor'
LEGACY_LAUNCHER_HASH = 'c47d681cf0eb27749e83163e6a3a5bcf4b399389932ec1ce0ec6211f7e9a4b86'


def _hash(data):
    return hashlib.sha256(data).hexdigest()


def _payload():
    package = Path(__file__).resolve().parent
    templates = package / 'skill_templates'
    files = {'SKILL.md': (templates / 'SKILL.md').read_bytes(),
             'scripts/monitor.py': (templates / 'monitor.py').read_bytes()}
    for path in sorted(package.glob('*.py')):
        files['scripts/codex_monitor/' + path.name] = path.read_bytes()
    for name in ('SKILL.md', 'monitor.py'):
        files['scripts/codex_monitor/skill_templates/' + name] = (templates / name).read_bytes()
    manifest = {'name': NAME, 'version': __version__, 'format': 1,
                'files': {name: _hash(data) for name, data in files.items()}}
    files['installation.json'] = (json.dumps(manifest, indent=2) + '\n').encode('utf-8')
    return files


def build_skill_archive(output=None):
    path = Path(output or f'dist/{NAME}-{__version__}-skill.zip').expanduser().resolve()
    files = _payload()
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + '.' + uuid.uuid4().hex + '.tmp')
    try:
        with zipfile.ZipFile(temp, 'w', compression=zipfile.ZIP_DEFLATED) as archive:
            for name in sorted(files):
                info = zipfile.ZipInfo(NAME + '/' + name, date_time=(2020, 1, 1, 0, 0, 0))
                info.compress_type = zipfile.ZIP_DEFLATED
                info.external_attr = 0o100644 << 16
                archive.writestr(info, files[name])
        os.replace(temp, path)
    finally:
        if temp.exists():
            temp.unlink()
    return path


def _is_link(path):
    # lstat reparse flags work on Python 3.11 too, before Path.is_junction exists.
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        return False
    return stat.S_ISLNK(metadata.st_mode) or bool(
        getattr(metadata, 'st_file_attributes', 0) & getattr(stat, 'FILE_ATTRIBUTE_REPARSE_POINT', 0x400))


def _check_existing(target):
    pending = [target]
    while pending:
        path = pending.pop()
        if _is_link(path):
            raise ValueError(f'Skill installation must not contain symbolic links or junctions: {path}')
        if path.is_dir():
            pending.extend(path.iterdir())
    manifest_path = target / 'installation.json'
    if manifest_path.is_file():
        manifest = json.loads(manifest_path.read_text(encoding='utf-8'))
        if manifest.get('name') != NAME or manifest.get('format') != 1 or not isinstance(manifest.get('files'), dict):
            raise ValueError('Unrecognized skill manifest; no files were changed')
        for name, digest in manifest['files'].items():
            relative = Path(name)
            path = target / relative
            if relative.is_absolute() or '..' in relative.parts or not path.resolve().is_relative_to(target.resolve()):
                raise ValueError('Invalid path in installed manifest')
            if path.is_symlink() or not path.is_file() or _hash(path.read_bytes()) != digest:
                raise ValueError(f'Installed file was customized or is missing: {name}; preserve your changes before upgrading')
        if not {'SKILL.md', 'scripts/monitor.py'}.issubset(manifest['files']):
            raise ValueError('Incomplete installation manifest')
        return
    launcher = target / 'scripts' / 'monitor.py'
    location = target / 'monitor-location.json'
    skill = target / 'SKILL.md'
    if (launcher.is_file() and location.is_file() and skill.is_file()
            and _hash(launcher.read_bytes()) == LEGACY_LAUNCHER_HASH
            and f'name: {NAME}' in skill.read_text(encoding='utf-8')):
        return
    raise ValueError('Destination is not a recognized codex-training-monitor installation')


@contextmanager
def _installation_lock(target):
    target.parent.mkdir(parents=True, exist_ok=True)
    lock = target.with_name('.' + target.name + '.install.lock')
    try:
        stream = lock.open('x', encoding='utf-8')
    except FileExistsError as exc:
        raise ValueError(f'Skill installation is already in progress: {lock}. If a previous installer crashed, verify it has exited before removing this lock.') from exc
    try:
        with stream:
            stream.write(str(os.getpid()))
        yield
    finally:
        lock.unlink()


def install_skill(destination=None, *, upgrade=False):
    codex_home = Path(os.environ.get('CODEX_HOME') or Path.home() / '.codex')
    requested = Path(destination or codex_home / 'skills' / NAME).expanduser().absolute()
    if _is_link(requested):
        raise ValueError('Skill destination must not be a symbolic link or junction')
    target = requested.resolve()
    with _installation_lock(target):
        return _install_payload(target, upgrade=upgrade)


def _install_payload(target, *, upgrade):
    files = _payload()
    if target.exists():
        _check_existing(target)
        if all((target / name).is_file() and (target / name).read_bytes() == data for name, data in files.items()):
            return {'status': 'unchanged', 'path': str(target), 'version': __version__}
        if not upgrade:
            raise ValueError('Existing installation differs; use --upgrade to preserve it in a backup and install this version')
    target.parent.mkdir(parents=True, exist_ok=True)
    stage = Path(tempfile.mkdtemp(prefix='.' + NAME + '-stage-', dir=target.parent)).resolve()
    backup = None
    try:
        for name, data in files.items():
            path = stage / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
        if target.exists():
            _check_existing(target)
            backup_root = (target.parent.parent if target.parent.name == 'skills' else target.parent) / 'skill-backups'
            backup_root.mkdir(parents=True, exist_ok=True)
            backup = backup_root / (target.name + '-' + uuid.uuid4().hex)
            target.rename(backup)
        try:
            stage.rename(target)
        except OSError:
            if backup is not None and not target.exists():
                backup.rename(target)
            raise
    finally:
        if stage.exists() and not stage.is_symlink() and stage.parent == target.parent and stage.name.startswith('.' + NAME + '-stage-'):
            shutil.rmtree(stage)
    result = {'status': 'installed', 'path': str(target), 'version': __version__}
    if backup:
        result['backup'] = str(backup)
    return result
