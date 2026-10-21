"""Reproduce MVP coverage gaps without modifying real projects or host settings.

Run from repository root: python -m reviews.verify_gaps
True means the named gap was reproduced, not that the feature works.
"""
import contextlib
import io
import json
import os
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

from agent_inventory.analyzer import analyze
from agent_inventory.discovery import source_paths, windows_entries, diagnostic
from agent_inventory.__main__ import scan, main


def verify():
    findings = {}
    with tempfile.TemporaryDirectory() as temporary:
        base = Path(temporary)
        root = base / 'project'
        root.mkdir()
        (root / 'agent-card.json').write_text('{"id":"review-fixture"}', encoding='utf-8')
        source = root / 'main.py'
        source.write_text('import openai\n', encoding='utf-8')
        config = {'roots': [], 'discover_host': True, 'output': str(base / 'out'), 'exclude': []}
        target = 'agent_inventory.discovery.' + ('windows_entries' if os.name == 'nt' else 'linux_entries')
        entry = {'kind': 'process', 'name': '123', 'command': 'python "{}"'.format(source), 'cwd': None}
        with patch(target, return_value=[entry]):
            scan(config)
        source.write_text('import openai\nimport redis\n', encoding='utf-8')
        with patch(target, return_value=[]):
            second = scan(config)
        inventory = json.loads((base / 'out/inventory.json').read_text(encoding='utf-8'))
        findings['stopped_discovered_project_edits_not_scanned'] = (
            second['observed'] == 0 and second['changed'] == 0
            and not any(item['value'] == 'redis' for item in inventory['cards'][0]['data_sources']))

        first = analyze(root, {})
        (root / 'execution-state.json').write_text('{"last_run":"new run"}', encoding='utf-8')
        next_card = analyze(root, {})
        findings['runtime_json_creates_source_revision'] = first['revision'] != next_card['revision']

        deep = root / 'one' / 'two'
        deep.mkdir(parents=True)
        (deep / 'hidden.py').write_text('import chromadb\n', encoding='utf-8')
        shallow = analyze(root, {'source_depth': 1})
        findings['source_depth_truncation_has_no_warning'] = (
            not shallow['warnings'] and not any(item['value'] == 'chromadb' for item in shallow['rag']))

        data = {'Processes': [{'ProcessId': 123, 'CommandLine': 'python main.py'}], 'Tasks': []}
        with patch('agent_inventory.discovery.run', return_value=json.dumps(data)), patch('agent_inventory.discovery.process_cwd', return_value=str(root)):
            entries = windows_entries([])
        findings['windows_relative_process_path_requires_missing_cwd'] = not source_paths(entries[0]['command'], entries[0]['cwd'])

        config_path = base / 'config.json'
        config_path.write_text(json.dumps(config), encoding='utf-8')
        def failed_tasks(warnings):
            diagnostic(warnings, 'schedule_query_failed', 'Scheduled task enumeration failed', 'error', 'windows-tasks')
            return []
        with patch(target, side_effect=failed_tasks), contextlib.redirect_stdout(io.StringIO()):
            code = main(['--config', str(config_path), 'scan'])
        findings['scheduler_enumeration_failure_exits_success'] = code == 0
    return findings


if __name__ == '__main__':
    findings = verify()
    print(json.dumps(findings, indent=2))
    sys.exit(1 if any(findings.values()) else 0)
