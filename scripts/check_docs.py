#!/usr/bin/env python3
"""Check public documentation structure and experiment configuration examples.

This is a static consistency check, not a secret scanner, Markdown renderer,
semantic review or proof that documented hardware/CLI commands work. Anchors,
external URLs and template destinations are deliberately not validated.
"""
import argparse
import html
import json
from pathlib import Path
import re
import subprocess
import sys
from urllib.parse import unquote, urlsplit


ENTRIES = (
    '.agents/skills/op-eval/SKILL.md',
    '.claude/skills/op-eval/SKILL.md',
    '.gemini/commands/op-eval.toml',
    '.opencode/commands/op-eval.md',
)
EXCLUDED_ROOT_DOCUMENTS = {'SERVER_VALIDATION.md'}
_FENCE = re.compile(r'^[ \t]{0,3}(`{3,}|~{3,})(.*)$')
_LINK = re.compile(r'\[[^\]\n]*\]\(\s*(?:<([^>\n]+)>|([^\s)]+))(?:\s+["\'][^\n]*?["\'])?\s*\)')
_REFERENCE = re.compile(r'^[ \t]{0,3}\[[^\]\n]+\]:\s*(?:<([^>\n]+)>|(\S+))', re.MULTILINE)


def _display(path, root):
    try:
        return path.relative_to(root).as_posix()
    except ValueError:
        return str(path)


def _content(path):
    text = path.read_text(encoding='utf8')
    if path.suffix != '.toml':
        return text
    # The managed Gemini entry emits its prompt using JSON-escaped TOML basic
    # string syntax. Decode it so embedded newlines/Markdown are checked too.
    prompt = re.search(r'^prompt\s*=\s*("(?:[^"\\]|\\.)*")\s*$', text, re.MULTILINE)
    if not prompt:
        raise ValueError('Cannot read the managed TOML prompt string')
    return json.loads(prompt.group(1))


def check_document(path, root):
    """Return relative-path diagnostics for one Markdown or managed TOML file."""
    path, root = Path(path), Path(root)
    label = _display(path, root)
    errors = []
    try:
        text = _content(path)
    except (OSError, UnicodeError, ValueError) as error:
        return [f'{label}: cannot read document ({type(error).__name__}: {error})']
    outside = []
    opening = None
    block = []
    for number, line in enumerate(text.splitlines(), 1):
        match = _FENCE.match(line)
        if opening is None:
            if match:
                token, info = match.groups()
                opening = (token[0], len(token), info.strip().split()[0].lower() if info.strip() else '', number)
                block = []
                outside.append('')
            else:
                outside.append(line)
        elif match and match.group(1)[0] == opening[0] and len(match.group(1)) >= opening[1] and not match.group(2).strip():
            if opening[2] == 'json':
                try:
                    json.loads('\n'.join(block))
                except ValueError as error:
                    errors.append(f'{label}:{opening[3]}: invalid JSON code block ({error})')
            opening = None
            outside.append('')
        else:
            block.append(line)
            outside.append('')
    if opening is not None:
        errors.append(f'{label}:{opening[3]}: unclosed code fence')
    prose = '\n'.join(outside)
    for expression in (_LINK, _REFERENCE):
        for match in expression.finditer(prose):
            target = html.unescape(match.group(1) or match.group(2))
            # Template placeholders are examples, not concrete filesystem paths.
            if not target or target.startswith('#') or any(mark in target for mark in ('<', '>', '{', '}', '$')):
                continue
            if match.group(1) is not None and re.fullmatch(r'[A-Z][A-Z0-9_]*', target):
                continue  # A bare <TASK_ID>/<ROOT> placeholder, not <file.md>.
            if target.startswith('//'):
                continue
            try:
                parsed = urlsplit(target)
            except ValueError:
                continue  # A renderer may accept other URL forms; do not guess.
            if parsed.scheme or parsed.netloc or not parsed.path:
                continue
            relative = unquote(parsed.path)
            destination = (root / relative.lstrip('/')) if relative.startswith('/') else (path.parent / relative)
            if not destination.exists():
                number = prose.count('\n', 0, match.start()) + 1
                errors.append(f'{label}:{number}: missing local link target: {target}')
    return errors


def check_experiment_examples(root, examples):
    """Load configs using this root's implementation, without executing a trial."""
    if not examples:
        return []
    program = '''import json, sys
from pathlib import Path
from experiment import load_config
errors = []
for name in json.load(sys.stdin):
    try:
        load_config(Path(name))
    except Exception as error:
        errors.append(name + ": invalid experiment configuration (" + type(error).__name__ + ": " + str(error) + ")")
print(json.dumps(errors, ensure_ascii=False))
'''
    try:
        result = subprocess.run([sys.executable, '-c', program], cwd=root,
                                input=json.dumps([p.relative_to(root).as_posix() for p in examples]),
                                text=True, capture_output=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired) as error:
        return [f'experiments/: config validation unavailable ({type(error).__name__})']
    if result.returncode:
        return ['experiments/: config validator failed to import or run; inspect the checked root and Python dependencies']
    try:
        errors = json.loads(result.stdout)
        if not isinstance(errors, list) or any(not isinstance(e, str) for e in errors):
            raise ValueError('Invalid checker output')
    except ValueError:
        return ['experiments/: config validator returned invalid output']
    return errors


def check_repository(root, *, validate_experiments=True):
    root = Path(root).resolve()
    errors = []
    if not root.is_dir():
        return {'documents_checked': 0, 'experiments_checked': 0, 'errors': ['.: repository root is not a directory']}
    files = {p for p in root.glob('*.md') if p.name not in EXCLUDED_ROOT_DOCUMENTS}
    files.update((root / 'docs').rglob('*.md'))
    for relative in ENTRIES:
        path = root / relative
        if path.is_file():
            files.add(path)
        else:
            errors.append(relative + ': managed Agent entry is missing')
    for path in sorted(files):
        errors.extend(check_document(path, root))
    examples = sorted((root / 'experiments').glob('*.json')) if validate_experiments else []
    errors.extend(check_experiment_examples(root, examples))
    return {'documents_checked': len(files), 'experiments_checked': len(examples), 'errors': errors}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, default=Path(__file__).resolve().parents[1],
                        help='Repository or exported public root (default: this script\'s repository)')
    args = parser.parse_args(argv)
    result = check_repository(args.root)
    if result['errors']:
        for error in result['errors']:
            print(error, file=sys.stderr)
        print(f"Documentation check failed: {len(result['errors'])} issue(s)", file=sys.stderr)
        return 1
    print(f"Documentation checks passed: {result['documents_checked']} documents, {result['experiments_checked']} experiment configurations")
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
