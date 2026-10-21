"""Read-only host discovery. Never execute discovered commands or source files."""
import json
import os
import re
import subprocess
from pathlib import Path

IGNORE = {'.git', '.venv', 'venv', 'node_modules', '__pycache__', '.inventory',
          'site-packages', 'dist', 'build', '.tox', '.mypy_cache', '.next', '.cache', '.backtest-cache'}
MARKERS = {'agent-card.json', 'pyproject.toml', 'package.json', 'requirements.txt', '.git'}
SCRIPTS = {'.py', '.js', '.ts', '.mjs', '.ps1', '.sh', '.bat', '.cmd'}


def diagnostic(issues, code, message, severity='warning', collector='host'):
    issues.append({'code': code, 'message': message, 'severity': severity, 'collector': collector})


def excluded_path(path, excluded):
    path = Path(path).resolve()
    return any(path == Path(e).resolve() or Path(e).resolve() in path.parents for e in excluded)


def run(args, timeout=30):
    result = subprocess.run(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            timeout=timeout, encoding='utf-8', errors='replace')
    if result.returncode:
        raise RuntimeError('command failed: ' + args[0])
    return result.stdout


def walk(root, depth=5, excluded=(), warnings=None, report_depth=False):
    root = Path(root).resolve()
    excluded = {str(Path(p).resolve()).casefold() for p in excluded}
    def error(exc):
        if warnings is not None:
            warnings.append('Unreadable directory: ' + str(exc.filename))
    for parent, dirs, files in os.walk(str(root), onerror=error, followlinks=False):
        here = Path(parent)
        level = len(here.relative_to(root).parts)
        allowed = sorted(d for d in dirs if d not in IGNORE
                         and not (here / d).is_symlink()
                         and str((here / d).resolve()).casefold() not in excluded)
        if level >= depth and allowed and report_depth and warnings is not None:
            warnings.append('Source depth limit reached: ' + str(here.relative_to(root)))
        dirs[:] = allowed if level < depth else []
        yield here, sorted(files), dirs


def project_for(source):
    current = source.parent
    for candidate in [current] + list(current.parents)[:5]:
        if any((candidate / marker).exists() for marker in MARKERS):
            return candidate
    return current


def source_paths(command, cwd=None):
    """Extract existing script paths, including quoted Windows paths; no shell evaluation."""
    tokens = re.findall(r'"([^"\r\n]+)"|\'([^\'\r\n]+)\'|([^\s"\']+)', command or '')
    result = []
    for parts in tokens:
        token = next(p for p in parts if p).rstrip(';,)')
        if Path(token).suffix.lower() not in SCRIPTS or any(c in token for c in ('$', '%', '`')):
            continue
        path = Path(token).expanduser()
        if not path.is_absolute():
            if not cwd:
                continue
            path = Path(cwd) / path
        if path.is_file():
            result.append(path.resolve())
    # Resolve local `python -m package` without importing the target package.
    match = re.search(r'(?:^|\s)-m\s+["\']?([A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*)', command or '')
    if match and cwd:
        module = Path(cwd).joinpath(*match.group(1).split('.'))
        for candidate in (module.with_suffix('.py'), module / '__main__.py'):
            if candidate.is_file():
                result.append(candidate.resolve())
                break
    return sorted(set(result), key=str)


def resolve_sources(command, cwd=None, seen=None, level=0):
    seen = seen if seen is not None else set()
    for source in source_paths(command, cwd):
        if source in seen:
            continue
        seen.add(source)
        if source.suffix.lower() in {'.bat', '.cmd', '.sh', '.ps1'} and level < 3:
            try:
                if source.stat().st_size > 256000:
                    continue
                wrapper_cwd = cwd or str(source.parent)
                for line in source.read_text(encoding='utf-8', errors='replace').splitlines():
                    line = line.replace('%~dp0', str(source.parent) + os.sep)
                    match = re.match(r'\s*(?:cd|Set-Location)\s+(?:/d\s+)?["\']?([^"\';&]+)', line, re.I)
                    if match:
                        next_dir = Path(match.group(1).strip())
                        if not next_dir.is_absolute():
                            next_dir = Path(wrapper_cwd) / next_dir
                        if next_dir.is_dir():
                            wrapper_cwd = str(next_dir.resolve())
                    resolve_sources(line, wrapper_cwd, seen, level + 1)
            except OSError:
                pass
    return sorted(seen, key=str)


def process_cwd(pid):
    """psutil reads process metadata only; target Python packages are never imported."""
    try:
        import psutil
    except ImportError:
        return None
    try:
        return psutil.Process(int(pid)).cwd()
    except (psutil.Error, OSError, ValueError):
        return None


def windows_entries(warnings):
    # Only execution metadata is queried. Raw command lines are never persisted.
    script = """$ErrorActionPreference='Stop'
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new()
$procs=@(Get-CimInstance Win32_Process | Select-Object ProcessId,CommandLine)
$taskError=$null
$tasks=@()
try { $tasks=@(Get-ScheduledTask | ForEach-Object {
  $t=$_
  foreach($a in $t.Actions) {
    [pscustomobject]@{Name=($t.TaskPath+$t.TaskName);Execute=$a.Execute;Arguments=$a.Arguments;Cwd=$a.WorkingDirectory;State=[string]$t.State}
  }
}) } catch { $taskError='Scheduled task enumeration failed' }
[pscustomobject]@{Processes=$procs;Tasks=$tasks;TaskError=$taskError}|ConvertTo-Json -Depth 5 -Compress
"""
    data = json.loads(run(['powershell.exe', '-NoProfile', '-NonInteractive', '-Command', script]))
    if data.get('TaskError'):
        diagnostic(warnings, 'schedule_query_failed', data['TaskError'], 'error', 'windows-tasks')
    entries = []
    for p in data.get('Processes') or []:
        command = p.get('CommandLine')
        relevant = command and re.search(r'\.(?:py|js|ts|mjs|ps1|sh|bat|cmd)\b|\s-m\s', command, re.I)
        entries.append({'kind': 'process', 'name': str(p['ProcessId']), 'command': command,
                        'cwd': process_cwd(p['ProcessId']) if relevant else None})
    for t in data.get('Tasks') or []:
        entries.append({'kind': 'schedule', 'name': t['Name'], 'state': t['State'],
                        'command': '"{}" {}'.format(t.get('Execute') or '', t.get('Arguments') or ''), 'cwd': t.get('Cwd')})
    return entries


def linux_entries(warnings):
    entries = []
    denied = 0
    for proc in Path('/proc').glob('[0-9]*'):
        try:
            args = (proc / 'cmdline').read_bytes().decode('utf-8', errors='replace').split('\0')
            entries.append({'kind': 'process', 'name': proc.name,
                            'command': ' '.join('"' + a.replace('"', '') + '"' for a in args),
                            'cwd': str((proc / 'cwd').resolve())})
        except OSError:
            denied += 1
    if denied:
        diagnostic(warnings, 'process_entries_unavailable', '{} /proc entries unavailable (permissions or exited process)'.format(denied), 'info', 'linux-processes')
    cron_sources = []
    try:
        cron_sources.append(('user crontab', run(['crontab', '-l']), str(Path.home())))
    except (OSError, RuntimeError, subprocess.TimeoutExpired):
        diagnostic(warnings, 'crontab_unavailable', 'User crontab unavailable or empty', 'warning', 'cron')
    for path in [Path('/etc/crontab')] + sorted(Path('/etc/cron.d').glob('*')):
        if path.is_file():
            try:
                cron_sources.append((str(path), path.read_text(errors='replace'), None))
            except OSError:
                diagnostic(warnings, 'cron_file_unreadable', 'Unreadable cron file: ' + str(path), 'warning', 'cron')
    for name, contents, cwd in cron_sources:
        for number, line in enumerate(contents.splitlines(), 1):
            if line.strip() and not line.lstrip().startswith('#'):
                entries.append({'kind': 'schedule', 'name': '{}:{}'.format(name, number), 'command': line, 'cwd': cwd})
    unit_roots = [Path('/etc/systemd/system'), Path('/usr/lib/systemd/system'), Path.home() / '.config/systemd/user']
    for root in unit_roots:
        for path in root.glob('*.service'):
            try:
                contents = path.read_text(errors='replace')
                cwd = re.search(r'^WorkingDirectory=(.+)$', contents, re.M)
                for command in re.findall(r'^ExecStart=(.+)$', contents, re.M):
                    entries.append({'kind': 'service-definition', 'name': str(path), 'command': command,
                                    'cwd': cwd.group(1).strip() if cwd else None})
            except OSError:
                diagnostic(warnings, 'unit_unreadable', 'Unreadable unit: ' + str(path), 'warning', 'systemd')
    return entries


def discover(config):
    warnings, candidates, runtime = [], {}, []
    def add(root, evidence):
        key = str(Path(root).resolve())
        candidates.setdefault(key, [])
        if evidence not in candidates[key]:
            candidates[key].append(evidence)
    for root in config.get('roots', []):
        if excluded_path(root, config.get('exclude', [])):
            continue
        if not Path(root).is_dir():
            diagnostic(warnings, 'root_missing', 'Missing configured root: ' + root, 'error', 'filesystem')
            continue
        path_warnings = []
        for path, files, dirs in walk(root, config.get('discovery_depth', 4), config.get('exclude', []), path_warnings):
            marked = bool(MARKERS.intersection(files) or '.git' in dirs or (path / '.git').exists())
            scripts = any(Path(f).suffix.lower() in SCRIPTS for f in files)
            if marked or scripts or 'SKILL.md' in files:
                add(path, {'kind': 'configured-root', 'source': str(path)})
                # Treat a marked repo as one candidate; runtime entries still locate nested scripts.
                if marked:
                    dirs[:] = []
        for warning in path_warnings:
            diagnostic(warnings, 'directory_unreadable', warning, 'warning', 'filesystem')
    if config.get('discover_host', True):
        try:
            entries = windows_entries(warnings) if os.name == 'nt' else linux_entries(warnings)
            unresolved, unresolved_processes = 0, 0
            for entry in entries:
                sources = resolve_sources(entry.get('command'), entry.get('cwd'))
                if not sources and entry['kind'] != 'process':
                    unresolved += 1
                if not sources and entry['kind'] == 'process' and re.search(r'\.(?:py|js|ts|mjs|ps1|sh|bat|cmd)\b|\s-m\s', entry.get('command') or '', re.I):
                    unresolved_processes += 1
                for source in sources:
                    root = project_for(source)
                    if excluded_path(root, config.get('exclude', [])):
                        continue
                    evidence = {'kind': entry['kind'], 'name': entry['name'], 'source': str(source)}
                    if entry.get('state'):
                        evidence['state'] = entry['state']
                    # Process PIDs live in runtime snapshots, not versioned source cards.
                    add(root, {'kind': 'host-source', 'source': str(source)})
                    runtime.append(dict(evidence, root=str(root)))
            if unresolved:
                diagnostic(warnings, 'schedule_sources_unresolved', '{} schedule/service entries have no resolvable script (binary/module/dynamic paths)'.format(unresolved), 'info')
            if unresolved_processes:
                diagnostic(warnings, 'process_sources_unresolved', '{} script/module process entries could not be resolved (cwd/permissions/dynamic paths)'.format(unresolved_processes), 'info')
        except (OSError, ValueError, RuntimeError, subprocess.TimeoutExpired) as exc:
            diagnostic(warnings, 'host_query_failed', 'Host discovery failed: ' + type(exc).__name__, 'error')
    return candidates, runtime, warnings
