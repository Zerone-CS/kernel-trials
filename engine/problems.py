"""Trusted operator packages; no candidate code is imported by the controller.

Package authors supply task text, fixed engineering, and a judge adapter.  The
framework supplies candidate snapshots, execution isolation, budgets and reports.
An adapter is executable administrator-installed code, not an untrusted upload.
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
import string
import sys
import types
import uuid
from typing import Any, Dict, List, Mapping, Optional


class ProblemError(ValueError):
    pass


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _relative(value: Any, label: str) -> str:
    if (not isinstance(value, str) or not value or value.startswith('/')
            or '\\' in value or any(ord(c) < 32 for c in value)):
        raise ProblemError(f'{label} must be a canonical package-relative path')
    parts = value.split('/')
    if any(part in ('', '.', '..') for part in parts):
        raise ProblemError(f'{label} must not contain empty, dot or parent components')
    return str(PurePosixPath(value))


def _inventory(root: Path) -> Dict[str, str]:
    """Hash every byte, including hidden files; never follow package symlinks."""
    if root.is_symlink() or not root.is_dir():
        raise ProblemError('Problem package must be a real directory')
    result = {}
    for path in sorted(root.rglob('*')):
        mode = path.lstat().st_mode
        if stat.S_ISLNK(mode):
            raise ProblemError(f'Symlink is forbidden in package: {path.relative_to(root)}')
        if stat.S_ISDIR(mode):
            continue
        if not stat.S_ISREG(mode):
            raise ProblemError(f'Special file is forbidden in package: {path.relative_to(root)}')
        result[path.relative_to(root).as_posix()] = _sha(path.read_bytes())
    return result


def _digest(files: Mapping[str, str]) -> str:
    return _sha(json.dumps(dict(files), sort_keys=True, ensure_ascii=True,
                           separators=(',', ':')).encode('ascii'))


def _json_file(root: Path, relative: str, label: str) -> Dict[str, Any]:
    try:
        data = json.loads((root / relative).read_text(encoding='utf8'))
    except (OSError, ValueError, UnicodeError) as error:
        raise ProblemError(f'Cannot read {label}: {relative}') from error
    if not isinstance(data, dict):
        raise ProblemError(f'{label} must be a JSON object')
    return data


def _command_template(value: Any) -> List[List[str]]:
    if not isinstance(value, list) or not value:
        raise ProblemError('build.commands must contain at least one argv list')
    result = []
    for command in value:
        if not isinstance(command, list) or not command:
            raise ProblemError('Each build command must be a nonempty argv list, not shell text')
        args = []
        for argument in command:
            if not isinstance(argument, str) or not argument or '\x00' in argument:
                raise ProblemError('Build arguments must be nonempty strings without NUL')
            try:
                for _, field, spec, conversion in string.Formatter().parse(argument):
                    if field is not None and (field not in ('project', 'build') or spec or conversion):
                        raise ProblemError('Only plain {project} and {build} placeholders are supported')
            except ValueError as error:
                raise ProblemError('Malformed build argument placeholder') from error
            args.append(argument)
        result.append(args)
    return result


def _policy(value: Any) -> Dict[str, Any]:
    if not isinstance(value, dict):
        raise ProblemError('candidate_policy is required')
    maximum = value.get('maximum_source_bytes')
    if type(maximum) is not int or not 1 <= maximum <= 16 * 1024 * 1024:
        raise ProblemError('maximum_source_bytes must be an integer between 1 and 16 MiB')
    headers = value.get('allowed_include_headers')
    if not isinstance(headers, list) or any(not isinstance(h, str) for h in headers):
        raise ProblemError('allowed_include_headers must be a list of literal header paths')
    for header in headers:
        _relative(header, 'allowed_include_headers entry')
    if len(headers) != len(set(headers)):
        raise ProblemError('allowed_include_headers contains duplicates')
    return {'maximum_source_bytes': maximum, 'allowed_include_headers': list(headers)}


def validate_candidate_source(source: bytes, policy: Mapping[str, Any]) -> Dict[str, Any]:
    """Conservative source gate, NOT an adversarial C++ security boundary.

    Literal include headers must have been allowed by the package author. Macro
    includes/include_next/import are rejected. A compiler sandbox must still
    prevent file access through assembler directives, pragmas, compiler plugins,
    macro tricks, and runtime calls; this check cannot prove source compliance.
    """
    rules = _policy(dict(policy))
    if not isinstance(source, bytes) or not source or len(source) > rules['maximum_source_bytes']:
        raise ProblemError('Candidate source is empty or exceeds its byte limit')
    try:
        text = source.decode('utf8')
    except UnicodeDecodeError as error:
        raise ProblemError('Candidate source must be UTF-8 text') from error
    if '\x00' in text:
        raise ProblemError('Candidate source must not contain NUL')
    text = re.sub(r'\\\r?\n', '', text)
    # Replace comments with whitespace/newlines so commented directives do not
    # accidentally become active directives in this inexpensive front gate.
    text = re.sub(r'/\*.*?\*/|//[^\n]*',
                  lambda m: ''.join('\n' if c == '\n' else ' ' for c in m.group()),
                  text, flags=re.DOTALL)
    included = []
    for match in re.finditer(r'^\s*#\s*(include_next|include|import)\b([^\n]*)', text, re.MULTILINE):
        if match.group(1) != 'include':
            raise ProblemError('include_next/import are not allowed in candidate source')
        literal = re.fullmatch(r'\s*(?:<([^<>]+)>|"([^"\n]+)")\s*', match.group(2))
        if not literal:
            raise ProblemError('Candidate include must be one literal header, not a macro expression')
        header = literal.group(1) or literal.group(2)
        _relative(header, 'candidate include')
        if header not in rules['allowed_include_headers']:
            raise ProblemError(f'Candidate header is not allowed: {header}')
        included.append(header)
    return {'source_sha256': _sha(source), 'source_bytes': len(source),
            'include_headers': included, 'security_boundary': False}


@dataclass(frozen=True)
class ProblemPackage:
    root: Path
    manifest: Dict[str, Any]
    digest: str
    file_hashes: Dict[str, str]

    @property
    def problem_id(self) -> str:
        return self.manifest['id']

    def assert_unchanged(self) -> None:
        if _inventory(self.root) != self.file_hashes:
            raise ProblemError('Problem package changed after registration/load')

    def path(self, key: str) -> Path:
        """Resolve an already validated top-level package file/directory field."""
        return self.root / _relative(self.manifest[key], key)

    def starter_path(self, track: str) -> Path:
        if track not in self.manifest['tracks']:
            raise ProblemError('Unknown task track')
        return self.root / self.manifest['tracks'][track]

    def load_adapter(self) -> types.ModuleType:
        self.assert_unchanged()
        path = self.path('adapter')
        module = types.ModuleType('op_eval_problem_' + self.digest + '_' + uuid.uuid4().hex)
        module.__file__ = str(path)
        # Compile administrator-supplied source directly: no __pycache__ output
        # is produced inside the immutable package. Adapters should be standalone
        # modules; helper/data files may be explicitly loaded relative to __file__.
        sys.modules[module.__name__] = module
        try:
            exec(compile(path.read_bytes(), str(path), 'exec'), module.__dict__)
            for name in ('cases', 'prepare', 'runner_args', 'verify'):
                if not callable(getattr(module, name, None)):
                    raise ProblemError(f'Trusted judge adapter is missing callable {name}()')
            self.assert_unchanged()
        except BaseException:
            sys.modules.pop(module.__name__, None)
            raise
        return module

    def stage_project(self, kernel_bytes: bytes, project_dir: Path) -> Dict[str, Any]:
        self.assert_unchanged()
        identity = validate_candidate_source(kernel_bytes, self.manifest['candidate_policy'])
        target = Path(project_dir)
        if target.is_symlink() or target.exists():
            raise ProblemError('Project snapshot destination must not already exist')
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(self.path('harness'), target)
        for relative, source in self.manifest.get('public_files', {}).items():
            destination = target / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(self.root / source, destination)
        (target / 'kernel.asc').write_bytes(kernel_bytes)
        return {**identity, 'problem_digest': self.digest,
                'project': str(target.resolve()), 'editable_file': 'kernel.asc'}

    def build_commands(self, project_dir: Path, build_dir: Path) -> List[List[str]]:
        context = {'project': str(Path(project_dir).resolve()), 'build': str(Path(build_dir).resolve())}
        return [[arg.format(**context) for arg in command]
                for command in self.manifest['build']['commands']]

    def runner_path(self, build_dir: Path) -> Path:
        return Path(build_dir).resolve() / self.manifest['build']['runner']


def load_problem(path: Path, expected_digest: Optional[str] = None) -> ProblemPackage:
    supplied = Path(path)
    if supplied.is_symlink():
        raise ProblemError('Problem package root must not be a symlink')
    root = supplied.resolve()
    files = _inventory(root)
    if 'problem.json' not in files:
        raise ProblemError('Missing problem.json')
    manifest = _json_file(root, 'problem.json', 'problem manifest')
    if manifest.get('schema_version') != 1:
        raise ProblemError('Unsupported problem schema_version')
    for key in ('id', 'version'):
        if not isinstance(manifest.get(key), str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,95}', manifest[key]):
            raise ProblemError(f'Invalid package {key}')
    if manifest.get('editable_file') != 'kernel.asc':
        raise ProblemError('The only editable submission file must be kernel.asc')
    for key in ('task', 'adapter', 'cases', 'protocol'):
        value = _relative(manifest.get(key), key)
        if value not in files:
            raise ProblemError(f'Missing required {key} file: {value}')
    if not manifest['adapter'].endswith('.py'):
        raise ProblemError('adapter must name a trusted Python source module')
    harness = _relative(manifest.get('harness'), 'harness')
    if not (root / harness).is_dir() or not any(k.startswith(harness + '/') for k in files):
        raise ProblemError('harness must be a nonempty fixed engineering directory')
    if (root / harness / 'kernel.asc').exists():
        raise ProblemError('Fixed harness must not contain the editable kernel.asc')
    tracks = manifest.get('tracks')
    if not isinstance(tracks, dict) or set(tracks) != {'from_scratch', 'optimize'}:
        raise ProblemError('tracks must define from_scratch and optimize starters')
    for track, value in tracks.items():
        value = _relative(value, f'track {track}')
        if value not in files:
            raise ProblemError(f'Missing starter for track {track}')
    if 'reference' in manifest:
        reference = _relative(manifest['reference'], 'reference')
        if reference not in files:
            raise ProblemError('Reference kernel is missing')
    public_files = manifest.get('public_files', {})
    if not isinstance(public_files, dict):
        raise ProblemError('public_files must map destination paths to package files')
    private_files = {manifest[k] for k in ('adapter', 'cases', 'protocol')}
    private_files.update(tracks.values())
    private_files.add('problem.json')
    if 'reference' in manifest:
        private_files.add(manifest['reference'])
    for destination, source in public_files.items():
        destination = _relative(destination, 'public_files destination')
        source = _relative(source, 'public_files source')
        if (source not in files or source in private_files or source.startswith(harness + '/')
                or destination == 'kernel.asc' or destination.startswith('kernel.asc/')
                or (root / harness / destination).exists()
                or any((root / harness / p).is_file() for p in PurePosixPath(destination).parents if str(p) != '.')):
            raise ProblemError('Public interface file is missing, private, or collides with fixed engineering')
    destinations = set(public_files)
    if any(str(parent) in destinations for destination in destinations
           for parent in PurePosixPath(destination).parents):
        raise ProblemError('Public interface destinations must not contain one another')
    build = manifest.get('build')
    if not isinstance(build, dict):
        raise ProblemError('build configuration is required')
    _command_template(build.get('commands'))
    _relative(build.get('runner'), 'build.runner')
    _policy(manifest.get('candidate_policy'))
    # Cases and protocol are explicit package inputs: a natural-language task
    # alone never silently becomes its own numerical oracle.
    _json_file(root, manifest['cases'], 'cases manifest')
    _json_file(root, manifest['protocol'], 'measurement protocol')
    digest = _digest(files)
    if expected_digest is not None and expected_digest != digest:
        raise ProblemError('Problem digest does not match the registered version')
    return ProblemPackage(root, manifest, digest, files)


def discover_problems(directory: Path) -> List[ProblemPackage]:
    root = Path(directory)
    if not root.is_dir() or root.is_symlink():
        raise ProblemError('Problem registry directory is missing or is a symlink')
    packages = []
    identities = set()
    for child in sorted(root.iterdir()):
        if child.is_symlink():
            raise ProblemError('Symlinked registry entries are not accepted')
        if not child.is_dir():
            continue
        if not (child / 'problem.json').is_file():
            continue
        package = load_problem(child)
        identity = (package.problem_id, package.manifest['version'])
        if identity in identities:
            raise ProblemError('Duplicate problem id/version in registry')
        identities.add(identity)
        packages.append(package)
    return packages
