"""Deterministic extraction with file/line evidence; no source execution."""
import ast
import hashlib
import fnmatch
import json
import re
import warnings as python_warnings
from pathlib import Path
from urllib.parse import urlsplit

from .discovery import run, walk

TEXT = {'.py', '.js', '.ts', '.tsx', '.mjs', '.json', '.toml', '.yaml', '.yml', '.md', '.txt', '.sh', '.ps1', '.bat', '.cmd'}
LIBRARIES = {'openai', 'anthropic', 'langchain', 'langgraph', 'crewai', 'autogen', 'llama_index', 'transformers'}
RAG = {'chromadb', 'chroma', 'qdrant_client', 'qdrant', 'pinecone', 'weaviate', 'faiss', 'pgvector', 'llama_index'}
DATABASES = {'sqlite3', 'psycopg2', 'pymongo', 'redis', 'sqlalchemy', 'pymysql', 'asyncpg'}
JSON_CONFIG = {'agent-card.json', 'package.json', 'package-lock.json', 'manifest.json',
               'mcp.json', '.mcp.json', 'settings.json', 'config.json', 'tools.json', 'skills.json', 'tsconfig.json'}


def source_file(path, relative, config):
    """Default JSON is configuration-only; runtime documents need no code revision."""
    name = path.name.lower()
    if any(fnmatch.fnmatch(relative.lower(), pattern.lower()) for pattern in config.get('source_exclude', [])):
        return False
    if any(fnmatch.fnmatch(relative.lower(), pattern.lower()) for pattern in config.get('source_include', [])):
        return True
    if path.suffix.lower() == '.json':
        return name in JSON_CONFIG or name.endswith(('.config.json', '.settings.json', '.schema.json'))
    if path.suffix.lower() == '.txt':
        return name.startswith(('requirements', 'readme', 'prompt', 'system', 'instruction'))
    return path.suffix.lower() in TEXT or path.name in {'CODEOWNERS', 'Dockerfile'}


def digest(value):
    return hashlib.sha256(value).hexdigest()


def field(value, source, method='extracted', line=None):
    result = {'value': value, 'source': source, 'method': method}
    if line is not None:
        result['line'] = line
    return result


def safe_text(text):
    text = re.sub(r'(?i)(api[_-]?key|password|secret|token|authorization)\s*[:=]\s*[^\s,;]+', r'\1=[REDACTED]', text)
    text = re.sub(r'https?://\S+', lambda m: safe_url(m.group(0)), text)
    return text[:600]


def safe_url(value):
    try:
        parsed = urlsplit(value)
        return '{}://{}'.format(parsed.scheme, parsed.hostname or '[unknown-host]')
    except ValueError:
        return '[invalid-url]'


def analyze(root, config):
    root = Path(root).resolve()
    files, warnings = {}, []
    max_files = config.get('max_files', 3000)
    max_bytes = config.get('max_file_bytes', 1000000)
    for parent, names, dirs in walk(root, config.get('source_depth', 12), config.get('exclude', []), warnings, report_depth=True):
        if len(files) >= max_files:
            warnings.append('File limit reached; inventory is partial')
            break
        for name in names:
            path = parent / name
            if path.is_symlink() or name.startswith('.env') or any(s in name.lower() for s in ('credential', 'secret', 'private_key')):
                continue
            relative = path.relative_to(root).as_posix()
            if not source_file(path, relative, config):
                continue
            if len(files) >= max_files:
                warnings.append('File limit reached; inventory is partial')
                break
            try:
                if path.stat().st_size > max_bytes:
                    warnings.append('Oversized file skipped: ' + str(path.relative_to(root)))
                    continue
                files[str(path.relative_to(root)).replace('\\', '/')] = path.read_bytes()
            except OSError:
                warnings.append('Unreadable file: ' + str(path.relative_to(root)))
    manifest = {}
    if 'agent-card.json' in files:
        try:
            manifest = json.loads(files['agent-card.json'].decode('utf-8-sig'))
            if not isinstance(manifest, dict):
                raise ValueError()
        except (ValueError, UnicodeError):
            warnings.append('Invalid agent-card.json')
            manifest = {}
    identity = manifest.get('id') if isinstance(manifest.get('id'), str) else str(root)
    card = {'schema_version': 1, 'id': digest(identity.encode('utf-8'))[:16], 'root': str(root),
            'name': field(root.name, 'directory'), 'purpose': None, 'owner': None,
            'skills': [], 'dependencies': [], 'mcp': [], 'api_hosts': [], 'rag': [],
            'data_sources': [], 'action_candidates': [], 'functions': [], 'prompt_sources': [],
            'deployment_sources': [], 'version': None, 'warnings': warnings,
            'analysis_mode': 'deterministic; inferred fields require review',
            'coverage': {'files_read': len(files), 'source_depth': config.get('source_depth', 12)}}
    for key in ('name', 'purpose', 'owner'):
        if isinstance(manifest.get(key), str):
            card[key] = field(safe_text(manifest[key]), 'agent-card.json', 'declared')
    dep_seen = set()
    def dependency(name, source, line):
        normalized = name.split('.')[0].lower().replace('-', '_')
        if (normalized, source) in dep_seen:
            return
        dep_seen.add((normalized, source))
        card['dependencies'].append(field(normalized, source, line=line))
        if normalized in RAG:
            card['rag'].append(field(normalized, source, 'dependency-candidate', line))
        if normalized in DATABASES:
            card['data_sources'].append(field(normalized, source, 'dependency-candidate', line))
    for source, raw in sorted(files.items()):
        content = raw.decode('utf-8-sig', errors='replace')
        name = Path(source).name.lower()
        lines = content.splitlines()
        if name.startswith('readme') and not card['purpose']:
            for i, line in enumerate(lines, 1):
                if line.strip() and not line.startswith(('#', '!', '[', '```', '|')):
                    card['purpose'] = field(safe_text(line.strip()), source, 'readme-excerpt', i)
                    break
        if name == 'codeowners' and not card['owner']:
            for i, line in enumerate(lines, 1):
                if line.strip().startswith('* '):
                    card['owner'] = field(safe_text(line.strip()[2:]), source, 'owner-candidate', i)
                    break
        if name == 'skill.md':
            match = re.search(r'^name:\s*(.+)', content, re.M)
            card['skills'].append(field(safe_text(match.group(1).strip()) if match else Path(source).parent.name, source, 'registered-file'))
        if any(term in name for term in ('prompt', 'instruction')) or name == 'agents.md':
            card['prompt_sources'].append(source)
        if any(term in source.lower() for term in ('.github/workflows/', 'dockerfile', 'docker-compose', '.gitlab-ci')):
            card['deployment_sources'].append(source)
        if name.endswith('.py'):
            try:
                with python_warnings.catch_warnings(record=True) as parse_warnings:
                    python_warnings.simplefilter('always', SyntaxWarning)
                    tree = ast.parse(content)
                if parse_warnings:
                    warnings.append('Python syntax warning: ' + source)
                for node in ast.walk(tree):
                    if isinstance(node, ast.Import):
                        for alias in node.names:
                            dependency(alias.name, source, node.lineno)
                    elif isinstance(node, ast.ImportFrom) and node.module:
                        dependency(node.module, source, node.lineno)
                    elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and not node.name.startswith('_'):
                        card['functions'].append(field(node.name, source, 'symbol', node.lineno))
            except (SyntaxError, ValueError):
                warnings.append('Python AST parse failed: ' + source)
        if name.startswith('requirements') and name.endswith('.txt'):
            for i, line in enumerate(lines, 1):
                match = re.match(r'^([A-Za-z][A-Za-z0-9_.-]*)', line)
                if match:
                    dependency(match.group(1), source, i)
        if name == 'package.json':
            try:
                package = json.loads(content)
                for group in ('dependencies', 'devDependencies'):
                    for dep in package.get(group, {}):
                        dependency(dep, source, None)
                if package.get('version'):
                    card['version'] = field(str(package['version']), source)
            except (ValueError, AttributeError, TypeError):
                warnings.append('Invalid package.json: ' + source)
        if name.endswith('.json'):
            try:
                data = json.loads(content)
                servers = data.get('mcpServers', data.get('mcp_servers', {})) if isinstance(data, dict) else {}
                if isinstance(servers, dict):
                    for server in servers:
                        card['mcp'].append(field(safe_text(server), source, 'configured'))
            except ValueError:
                pass
        if name.endswith('.toml'):
            for i, line in enumerate(lines, 1):
                match = re.match(r'\s*\[mcp_servers\.([\w-]+)\]', line)
                if match:
                    card['mcp'].append(field(match.group(1), source, 'configured', i))
            if name == 'pyproject.toml':
                # Only dependency-array entries; no evaluation of TOML or build hooks.
                match = re.search(r'^dependencies\s*=\s*\[([\s\S]*?)\]', content, re.M)
                if match:
                    for dep in re.findall(r'["\']([A-Za-z][A-Za-z0-9_.-]*)', match.group(1)):
                        dependency(dep, source, None)
        for i, line in enumerate(lines, 1):
            # Configured URLs are represented by host only, never credentials/query/path.
            if not name.endswith(('.md', '.txt')):
                for url in re.findall(r'https?://[^\s"\'<>]+', line):
                    item = field(safe_url(url), source, 'reference-candidate', i)
                    if item not in card['api_hosts']:
                        card['api_hosts'].append(item)
            if name.endswith(('.py', '.js', '.ts', '.tsx', '.mjs')):
                for label, pattern in [('read', r'\b(?:read_text|read_csv|read_sql|read_file|fetch|search|query)\s*\('),
                                       ('write', r'\b(?:write_text|write_bytes|to_csv|insert|update|delete|unlink|remove)\s*\('),
                                       ('generate', r'\b(?:generate|create_completion|invoke)\s*\(')]:
                    if re.search(pattern, line):
                        card['action_candidates'].append(field(label, source, 'pattern-candidate', i))
    if (root / '.git').exists():
        try:
            card['version'] = field(run(['git', '-C', str(root), 'rev-parse', 'HEAD'], 5).strip(), '.git/HEAD')
        except Exception:
            warnings.append('Git revision unavailable')
    names = {x['value'] for x in card['dependencies']}
    card['classification'] = 'agent-candidate' if (manifest or card['skills'] or card['mcp'] or names.intersection(LIBRARIES)) else 'automation-candidate'
    card['source_hash'] = digest(json.dumps({p: digest(b) for p, b in sorted(files.items())}, sort_keys=True).encode('utf-8'))
    card['revision'] = digest(json.dumps(card, sort_keys=True, ensure_ascii=False).encode('utf-8'))[:16]
    return card
