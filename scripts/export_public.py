#!/usr/bin/env python3
"""Export a reviewed source allowlist to a new standalone repository directory.

This never publishes, initializes Git, chooses a license, or cleans the source.
The allowlist is a packaging boundary, not a content/secret or rights audit.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import stat


ROOT_FILES = (
    'admin.py', 'assigned.py', 'client.py', 'experiment.py',
    'experiment_remote.py', 'experiment_runner.py', 'launch.py',
    'launch_routes.py', 'service.py', 'workspace.py',
    'README.md', 'RESEARCH_GUIDE.md', 'EVALUATION_POLICY.md',
    'SECURITY_DESIGN.md', 'TASK_CATALOG.md', 'CONTRIBUTING.md',
    'SECURITY.md', 'THIRD_PARTY.md', 'requirements-npu.txt', '.gitignore',
)
DIRECTORIES = ('engine', 'tests', 'problems', 'docs', 'experiments', '.github')
ENTRY_FILES = (
    '.agents/skills/op-eval/SKILL.md', '.claude/skills/op-eval/SKILL.md',
    '.gemini/commands/op-eval.toml', '.opencode/commands/op-eval.md',
)
SCRIPT_FILES = tuple('scripts/' + name for name in (
    'export_public.py', 'check_docs.py', 'claude_smoke.py',
    'claude_auto_launch_smoke.py', 'npu_negative_smoke.py', 'research_smoke.py',
))
OPTIONAL_ROOT_FILES = ('LICENSE', 'LICENSE.md', 'LICENSE.txt',
                       'NOTICE', 'NOTICE.md', 'NOTICE.txt')
EXTENSIONS = frozenset(('.py', '.md', '.json', '.txt', '.asc', '.h', '.cpp',
                        '.yml', '.yaml', '.toml'))
SPECIAL_NAMES = frozenset(('CMakeLists.txt', 'LICENSE', 'NOTICE', '.gitignore'))
EXCLUDED_DIRECTORIES = frozenset((
    '.git', '.eval', '.evals', '__pycache__', '.pytest_cache', '.mypy_cache',
    '.ruff_cache', '.venv', 'venv', 'node_modules', 'workspaces', 'evidence',
    'logs', 'runs', 'build', 'dist', 'tmp', 'cache', 'private', 'secrets',
    'credentials', 'state', 'judge-state', 'task_packages', 'op_eval_experiment_runs',
))
EXCLUDED_NAMES = frozenset((
    'SERVER_VALIDATION.md', 'PUBLIC_MANIFEST.json', 'launcher.json',
    'session.json', 'settings.json', 'settings.local.json', 'credentials.json',
    'secrets.json', 'secret.json', 'auth.json', 'token.json', 'tokens.json', '.DS_Store',
))


class ExportError(ValueError):
    pass


def _safe_name(relative):
    if any(ord(c) < 32 for c in relative) or '\\' in relative:
        raise ExportError('Noncanonical source path: ' + repr(relative))


def _eligible(relative):
    path = Path(relative)
    if any(part in EXCLUDED_DIRECTORIES for part in path.parts[:-1]):
        return False
    if path.name in EXCLUDED_NAMES or path.name.startswith('.env'):
        return False
    if path.name.endswith(('.local.json', '.local.toml', '.local.yaml', '.local.yml')):
        return False
    return path.suffix.lower() in EXTENSIONS or path.name in SPECIAL_NAMES


def _read_regular(root, relative):
    _safe_name(relative)
    path = root
    for component in Path(relative).parts:
        path = path / component
        try:
            info = path.lstat()
        except FileNotFoundError:
            raise ExportError('Required public source is missing: ' + relative) from None
        if stat.S_ISLNK(info.st_mode):
            raise ExportError('Refusing selected symlink: ' + relative)
    if not stat.S_ISREG(info.st_mode):
        raise ExportError('Selected source is not a regular file: ' + relative)
    descriptor = os.open(str(path), os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0)
                         | getattr(os, 'O_NONBLOCK', 0))
    with os.fdopen(descriptor, 'rb') as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode):
            raise ExportError('Selected source is not a regular file: ' + relative)
        data = stream.read()
    return data, (0o755 if info.st_mode & 0o111 else 0o644)


def _walk(root, relative, selected, excluded):
    directory = root / relative
    if directory.is_symlink() or not directory.is_dir():
        raise ExportError('Required public source directory is missing or symlinked: ' + relative)
    for entry in sorted(directory.iterdir(), key=lambda path: path.name):
        name = entry.relative_to(root).as_posix()
        _safe_name(name)
        info = entry.lstat()
        if entry.name in EXCLUDED_DIRECTORIES:
            excluded.append(name)
            continue
        if stat.S_ISLNK(info.st_mode):
            raise ExportError('Refusing selected symlink: ' + name)
        if stat.S_ISDIR(info.st_mode):
            _walk(root, name, selected, excluded)
        elif stat.S_ISREG(info.st_mode):
            if _eligible(name):
                selected[name] = _read_regular(root, name)
            else:
                excluded.append(name)
        else:
            raise ExportError('Special file in selected source directory: ' + name)


def _problem_identities(selected, excluded):
    result = []
    for manifest in sorted(name for name in selected
                           if name.startswith('problems/') and name.endswith('/problem.json')):
        directory = str(Path(manifest).parent)
        omitted = [name for name in excluded if name == directory or name.startswith(directory + '/')]
        if omitted:
            raise ExportError('Cannot preserve problem package identity while filtering: '
                              + ', '.join(omitted)
                              + '. Do not clean a calibrated package; prepare a reviewed new version.')
        hashes = {name[len(directory) + 1:]: hashlib.sha256(data).hexdigest()
                  for name, (data, _) in selected.items() if name.startswith(directory + '/')}
        digest = hashlib.sha256(json.dumps(hashes, sort_keys=True, ensure_ascii=True,
                                           separators=(',', ':')).encode('ascii')).hexdigest()
        result.append({'path': directory, 'file_count': len(hashes), 'package_digest': digest})
    return result


def export_public(source, output, *, dry_run=False):
    source = Path(source)
    if source.is_symlink() or not source.is_dir():
        raise ExportError('Source root must be a real directory')
    source = source.resolve()
    supplied_output = Path(output).expanduser()
    if supplied_output.exists() or supplied_output.is_symlink():
        raise ExportError('Output must be a new path; refusing overwrite')
    output = supplied_output.resolve()
    if output == source or output.is_relative_to(source):
        raise ExportError('Output must be outside the source tree')
    selected, excluded = {}, []
    for name in ROOT_FILES + ENTRY_FILES + SCRIPT_FILES:
        selected[name] = _read_regular(source, name)
    for name in OPTIONAL_ROOT_FILES:
        if (source / name).exists() or (source / name).is_symlink():
            selected[name] = _read_regular(source, name)
    for name in DIRECTORIES:
        _walk(source, name, selected, excluded)
    packages = _problem_identities(selected, excluded)
    licenses = [name for name in OPTIONAL_ROOT_FILES if name.startswith('LICENSE') and name in selected]
    notices = [name for name in OPTIONAL_ROOT_FILES if name.startswith('NOTICE') and name in selected]
    manifest = {
        'schema_version': 1, 'kind': 'op_eval_public_export',
        'layout': 'standalone_repository_root',
        'license': {'present': bool(licenses), 'files': licenses,
                    'notice_files': notices, 'selected_by_exporter': False},
        'files': [{'path': name, 'size_bytes': len(data), 'sha256': hashlib.sha256(data).hexdigest()}
                  for name, (data, _) in sorted(selected.items())],
        'problem_packages': packages,
        'review_required': ['secret_and_private_context_review',
                            'source_and_method_resource_rights_review',
                            'project_license_review' if licenses else 'project_license_decision'],
    }
    if not dry_run:
        output.parent.mkdir(parents=True, exist_ok=True)
        # The exclusive mkdir is the overwrite guard, including races after
        # preflight. All selected bytes are already read before creating it.
        try:
            output.mkdir()
        except FileExistsError:
            raise ExportError('Output appeared during export; refusing overwrite') from None
        for name, (data, mode) in sorted(selected.items()):
            target = output / name
            target.parent.mkdir(parents=True, exist_ok=True)
            descriptor = os.open(str(target), os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
            with os.fdopen(descriptor, 'wb') as stream:
                stream.write(data)
        # Written last; missing manifest means an incomplete export. No source
        # or partially written destination is silently deleted on I/O failure.
        with (output / 'PUBLIC_MANIFEST.json').open('x', encoding='utf8') as stream:
            json.dump(manifest, stream, ensure_ascii=False, indent=2)
            stream.write('\n')
    return manifest


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', required=True, help='New directory outside this source tree')
    parser.add_argument('--dry-run', action='store_true', help='Validate and summarize without writing output')
    args = parser.parse_args(argv)
    try:
        manifest = export_public(Path(__file__).resolve().parents[1], args.output, dry_run=args.dry_run)
    except (ExportError, OSError) as error:
        parser.exit(1, 'Export refused: ' + str(error) + '\n')
    print(json.dumps({'dry_run': args.dry_run, 'file_count': len(manifest['files']),
                      'problem_packages': manifest['problem_packages'],
                      'license': manifest['license'], 'published': False}, ensure_ascii=False, indent=2))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
