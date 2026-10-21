import json
import tempfile
import unittest
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch
from types import SimpleNamespace

from agent_inventory.analyzer import analyze
from agent_inventory.discovery import discover, resolve_sources, windows_entries, diagnostic
from agent_inventory.registry import Registry
from agent_inventory.__main__ import scan, calculate_kpi, main
from agent_inventory.locking import scan_lock
from agent_inventory.scheduled import main as scheduled_main


class InventoryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name).resolve()
        self.root = self.base / 'agent with spaces'
        self.root.mkdir()
        self.write('agent-card.json', json.dumps({'id': 'test-agent', 'name': 'Test', 'owner': 'Team'}))
        self.write('main.py', 'import openai\nimport chromadb\nimport sqlite3\ndef search(q):\n    return q\n')
        self.config = {'roots': [str(self.root)], 'output': str(self.base / 'out'), 'discover_host': False, 'exclude': []}
        self.registry = Registry(self.base / 'db.sqlite3')

    def tearDown(self):
        self.registry.db.close()
        self.temp.cleanup()

    def write(self, name, contents):
        target = self.root / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(contents, encoding='utf-8')

    def test_evidence_extraction_and_no_source_execution(self):
        self.write('danger.py', 'raise RuntimeError("must never execute")\n')
        self.write('mcp.json', '{"mcpServers":{"search":{"env":{"API_KEY":"supersecret"}}}}')
        self.write('.env', 'SECRET=supersecret')
        self.write('skills/search/SKILL.md', '---\nname: search\n---\n')
        card = analyze(self.root, self.config)
        self.assertEqual(card['classification'], 'agent-candidate')
        self.assertEqual(card['rag'][0]['value'], 'chromadb')
        self.assertEqual(card['rag'][0]['line'], 2)
        self.assertEqual(card['owner']['method'], 'declared')
        self.assertEqual(card['skills'][0]['value'], 'search')
        self.assertEqual(card['mcp'][0]['value'], 'search')
        self.assertNotIn('supersecret', json.dumps(card))

    def test_purpose_prefers_root_readme_and_keeps_wrapped_paragraph(self):
        self.write('.pytest_cache/README.md', '# Cache\n\nUnrelated cache description.')
        self.write('cloud/README.md', '# Relay\n\nDeployment helper.')
        self.write('README.md', '# Golf monitor\n\nChecks tee times\nand sends alerts.\n\n## Install\nRun setup.')
        purpose = analyze(self.root, self.config)['purpose']
        self.assertEqual(purpose['value'], 'Checks tee times and sends alerts.')
        self.assertEqual((purpose['source'], purpose['line']), ('README.md', 3))

    def test_purpose_uses_explicit_section_in_other_markdown(self):
        self.write('AGENTS.md', '# Rules\n\nNever execute target code.')
        self.write('docs/design.md', '# Design\n\nDraft notes.\n\n## 목적\n음성 파일을 변환하고\n텍스트를 저장합니다.\n\n## 설치\nsetup')
        purpose = analyze(self.root, self.config)['purpose']
        self.assertEqual(purpose['value'], '음성 파일을 변환하고 텍스트를 저장합니다.')
        self.assertEqual(purpose['source'], 'docs/design.md')

    def test_purpose_skips_cache_backup_and_install_commands(self):
        self.write('.pytest_cache/README.md', 'Cache description')
        self.write('github-backup-staging/README.md', 'Backup description')
        self.write('README.md', '# Project\n\n## Install\n```sh\nrun installer\n```\n\n## Features\n- Detect changes\n- Send alerts\n')
        self.assertEqual(analyze(self.root, self.config)['purpose']['value'], '- Detect changes - Send alerts')
        (self.root / 'README.md').unlink()
        self.assertIsNone(analyze(self.root, self.config)['purpose'])

    def test_declared_purpose_takes_precedence_over_documents(self):
        self.write('agent-card.json', '{"purpose":"Reviewed purpose"}')
        self.write('README.md', '# Project\n\nUnreviewed description.')
        self.assertEqual(analyze(self.root, self.config)['purpose']['method'], 'declared')

    def test_plain_readme_title_does_not_replace_description(self):
        self.write('README.md', '레슨 알림 사용 매뉴얼\n\n빈자리를 확인하고\n알림을 보냅니다.\n')
        self.assertEqual(analyze(self.root, self.config)['purpose']['value'], '빈자리를 확인하고 알림을 보냅니다.')

    def test_changes_preserve_approved_snapshot_and_require_new_review(self):
        first = analyze(self.root, self.config)
        self.assertTrue(self.registry.save(first))
        self.registry.approve(first['id'], first['revision'], 'Reviewer')
        self.assertFalse(self.registry.save(analyze(self.root, self.config)))
        self.assertEqual(self.registry.cards()[0]['status'], 'approved')
        self.write('main.py', 'import openai\ndef delete_record():\n    return None\n')
        second = analyze(self.root, self.config)
        self.assertEqual(first['id'], second['id'])
        self.assertTrue(self.registry.save(second))
        card = self.registry.cards()[0]
        self.assertEqual(card['status'], 'draft')
        self.assertEqual(card['published_revision'], first['revision'])
        with self.assertRaises(ValueError):
            self.registry.approve(second['id'], first['revision'], 'Reviewer')
        self.registry.export(self.base / 'export')
        published = json.loads((self.base / 'export/published.json').read_text(encoding='utf-8'))
        self.assertEqual(published[0]['revision'], first['revision'])

    def test_deletion_changes_revision(self):
        self.write('extra.py', 'import redis')
        first = analyze(self.root, self.config)
        (self.root / 'extra.py').unlink()
        second = analyze(self.root, self.config)
        self.assertNotEqual(first['revision'], second['revision'])
        self.assertFalse(any(d['value'] == 'redis' for d in second['data_sources']))

    def test_schedule_wrapper_with_spaces_and_cd(self):
        wrapper = self.base / 'launch.bat'
        wrapper.write_text('cd /d "{}"\npython main.py --token secret\n'.format(self.root), encoding='utf-8')
        sources = resolve_sources('cmd /c "{}"'.format(wrapper))
        self.assertIn(self.root / 'main.py', sources)
        self.assertIn(wrapper, sources)

    def test_runtime_discovery_does_not_persist_arguments(self):
        entry = {'kind': 'schedule', 'name': 'nightly', 'command': 'python "{}" --token SECRET123'.format(self.root / 'main.py'), 'cwd': None}
        target = 'agent_inventory.discovery.windows_entries' if __import__('os').name == 'nt' else 'agent_inventory.discovery.linux_entries'
        with patch(target, return_value=[entry]):
            candidates, runtime, warnings = discover(dict(self.config, roots=[], discover_host=True))
        self.assertIn(str(self.root), candidates)
        self.assertEqual(runtime[0]['kind'], 'schedule')
        self.assertNotIn('SECRET123', json.dumps([candidates, runtime, warnings]))

    def test_runtime_pid_does_not_change_revision(self):
        first = analyze(self.root, self.config)
        self.registry.save(first)
        self.registry.snapshot({'runtime': [{'name': '12', 'root': str(self.root)}]})
        second = analyze(self.root, self.config)
        self.assertEqual(first['revision'], second['revision'])

    def test_scan_idempotence_and_missing_source(self):
        self.assertEqual(scan(self.config)['changed'], 1)
        self.assertEqual(scan(self.config)['changed'], 0)
        self.root.rename(self.base / 'moved-project')
        scan(dict(self.config, roots=[]))
        result = json.loads((self.base / 'out/inventory.json').read_text(encoding='utf-8'))
        self.assertFalse(result['cards'][0]['observed_in_latest_scan'])

    def test_usage_dedup_invalid_and_conflicting_events(self):
        card = analyze(self.root, self.config)
        self.registry.save(card)
        event = {'event_id': 'run-1', 'agent_id': card['id'], 'timestamp': '2026-10-21T10:00:00Z',
                 'status': 'success', 'duration_ms': 100, 'user_hash': 'pseudonym', 'tools': ['mcp:search', 'mcp:search'], 'secret': 'DROP_ME'}
        path = self.base / 'events.jsonl'
        path.write_text(json.dumps(event) + '\n' + json.dumps(event) + '\n{}\n', encoding='utf-8')
        result = self.registry.ingest(path)
        self.assertEqual(result['accepted'], 1)
        self.assertEqual(result['duplicates'], 1)
        self.assertEqual(len(result['errors']), 1)
        self.assertNotIn('DROP_ME', self.registry.db.execute('SELECT body FROM events').fetchone()[0])
        event['status'] = 'failure'
        path.write_text(json.dumps(event), encoding='utf-8')
        self.assertEqual(len(self.registry.ingest(path)['errors']), 1)
        usage = self.registry.usage(card['id'])
        self.assertEqual(usage['executions'], 1)
        self.assertEqual(usage['tools']['mcp:search'], 2)
        self.assertEqual(usage['success_rate'], 1)

    def test_kpi_requires_measured_definition(self):
        data = {'name': 'TAT', 'unit': 'min', 'direction': 'lower_is_better', 'before': 60, 'after': 40,
                'baseline_source': 'before.csv', 'after_source': 'after.csv', 'baseline_period': 'Sep',
                'after_period': 'Oct', 'cohort': 'support', 'approved_by': 'Reviewer'}
        self.assertEqual(calculate_kpi(data)['improvement_percent'], 33.3333)
        with self.assertRaises(ValueError):
            calculate_kpi(dict(data, before=0))
        with self.assertRaises(ValueError):
            calculate_kpi(dict(data, after=float('nan')))
        with self.assertRaises(ValueError):
            calculate_kpi(dict(data, baseline_source=''))

    def test_html_escapes_untrusted_metadata(self):
        self.write('agent-card.json', json.dumps({'name': '<script>alert(1)</script>'}))
        self.registry.save(analyze(self.root, self.config))
        self.registry.export(self.base / 'html')
        page = (self.base / 'html/index.html').read_text(encoding='utf-8')
        self.assertNotIn('<script>alert(1)</script>', page)
        self.assertIn('&lt;script&gt;', page)

    def test_url_credentials_and_query_removed(self):
        self.write('api.py', 'URL="https://user:password@example.com/private?token=abc"')
        card = analyze(self.root, self.config)
        self.assertEqual(card['api_hosts'][0]['value'], 'https://example.com')
        self.assertNotIn('password', json.dumps(card))

    def test_limits_visible_and_partial_cannot_be_approved(self):
        card = analyze(self.root, dict(self.config, max_files=1))
        self.assertTrue(card['warnings'])
        self.registry.save(card)
        with self.assertRaises(ValueError):
            self.registry.approve(card['id'], card['revision'], 'Reviewer')

    def test_excluded_directory_is_not_scanned(self):
        nested = self.root / 'ignored'
        self.write('ignored/hidden.py', 'import redis')
        card = analyze(self.root, dict(self.config, exclude=[str(nested)]))
        self.assertFalse(any(d['value'] == 'redis' for d in card['data_sources']))

    def test_watch_cli_detects_edit_between_cycles(self):
        config_path = self.base / 'config.json'
        config_path.write_text(json.dumps(self.config), encoding='utf-8')
        process = subprocess.Popen([sys.executable, '-m', 'agent_inventory', '--config', str(config_path),
                                    'watch', '--interval', '1', '--cycles', '2'],
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True, encoding='utf-8')
        try:
            first = json.loads(process.stdout.readline())
            self.assertEqual(first['changed'], 1)
            self.write('main.py', 'import openai\ndef new_feature():\n    return True\n')
            stdout, stderr = process.communicate(timeout=20)
            self.assertEqual(process.returncode, 0, stderr)
            self.assertEqual(json.loads(stdout.strip())['changed'], 1)
        finally:
            if process.poll() is None:
                process.kill()
                process.communicate()

    def test_toml_mcp_and_pyproject_dependencies(self):
        self.write('config.toml', '[mcp_servers.docs]\ncommand="example"\n')
        self.write('pyproject.toml', '[project]\ndependencies = ["qdrant-client>=1", "anthropic"]\n')
        card = analyze(self.root, self.config)
        self.assertEqual(card['mcp'][0]['value'], 'docs')
        self.assertTrue(any(item['value'] == 'qdrant_client' for item in card['rag']))

    def test_stopped_process_project_is_still_tracked(self):
        config = dict(self.config, roots=[], discover_host=True)
        target = 'agent_inventory.discovery.' + ('windows_entries' if __import__('os').name == 'nt' else 'linux_entries')
        entry = {'kind': 'process', 'name': '42', 'command': 'python "{}"'.format(self.root / 'main.py'), 'cwd': None}
        with patch(target, return_value=[entry]):
            scan(config)
        self.write('main.py', 'import openai\nimport redis\n')
        with patch(target, return_value=[]):
            result = scan(config)
        self.assertEqual(result['observed'], 1)
        self.assertEqual(result['changed'], 1)
        card = json.loads((self.base / 'out/inventory.json').read_text(encoding='utf-8'))['cards'][0]
        self.assertTrue(any(item['value'] == 'redis' for item in card['data_sources']))
        self.assertEqual(card['runtime'], [])

    def test_exclusion_removes_previously_discovered_root_from_scans(self):
        scan(self.config)
        self.write('main.py', 'import redis')
        result = scan(dict(self.config, exclude=[str(self.root)]))
        self.assertEqual(result['observed'], 0)

    def test_runtime_json_does_not_invalidate_approval(self):
        first = analyze(self.root, self.config)
        self.registry.save(first)
        self.registry.approve(first['id'], first['revision'], 'Reviewer')
        self.write('execution-state.json', '{"count":123}')
        self.write('random-output.json', '{"count":456}')
        self.assertFalse(self.registry.save(analyze(self.root, self.config)))
        self.assertEqual(self.registry.cards()[0]['status'], 'approved')
        self.write('settings.json', '{"model":"changed"}')
        self.assertTrue(self.registry.save(analyze(self.root, self.config)))
        self.assertEqual(self.registry.cards()[0]['status'], 'draft')

    def test_custom_json_can_be_explicitly_included(self):
        config = dict(self.config, source_include=['custom-tool.json'])
        first = analyze(self.root, config)
        self.write('custom-tool.json', '{"description":"new tool"}')
        self.assertNotEqual(first['revision'], analyze(self.root, config)['revision'])

    def test_depth_limit_warns_and_blocks_approval(self):
        self.write('one/two/hidden.py', 'import chromadb')
        card = analyze(self.root, dict(self.config, source_depth=1))
        self.assertTrue(any('depth limit' in w for w in card['warnings']))
        self.registry.save(card)
        with self.assertRaises(ValueError):
            self.registry.approve(card['id'], card['revision'], 'Reviewer')

    def test_windows_collector_reads_cwd_for_relative_script(self):
        data = {'Processes': [{'ProcessId': 42, 'CommandLine': 'python main.py'}], 'Tasks': []}
        fake_psutil = SimpleNamespace(Process=lambda pid: SimpleNamespace(cwd=lambda: str(self.root)), Error=RuntimeError)
        with patch('agent_inventory.discovery.run', return_value=json.dumps(data)), patch.dict(sys.modules, {'psutil': fake_psutil}):
            entries = windows_entries([])
        self.assertEqual(entries[0]['cwd'], str(self.root))
        self.assertIn(self.root / 'main.py', resolve_sources(entries[0]['command'], entries[0]['cwd']))

    def test_module_resolution_does_not_execute_package(self):
        self.write('pkg/__init__.py', 'raise RuntimeError("do not import")')
        self.write('pkg/__main__.py', 'raise RuntimeError("do not execute")')
        self.assertIn(self.root / 'pkg/__main__.py', resolve_sources('python -m pkg', str(self.root)))

    def test_schedule_query_error_is_structured_and_nonzero(self):
        import contextlib
        import io
        config = dict(self.config, discover_host=True)
        path = self.base / 'config.json'
        path.write_text(json.dumps(config), encoding='utf-8')
        target = 'agent_inventory.discovery.' + ('windows_entries' if __import__('os').name == 'nt' else 'linux_entries')
        def failed_tasks(issues):
            diagnostic(issues, 'schedule_query_failed', 'Scheduled task enumeration failed', 'error', 'windows-tasks')
            return []
        with patch(target, side_effect=failed_tasks), contextlib.redirect_stdout(io.StringIO()):
            code = main(['--config', str(path), 'scan'])
        self.assertEqual(code, 2)
        saved = json.loads((self.base / 'out/last-scan.json').read_text(encoding='utf-8'))
        self.assertEqual(saved['status'], 'partial')
        self.assertEqual(saved['issues'][0]['code'], 'schedule_query_failed')

    def test_second_scan_skips_while_first_owns_lock(self):
        with scan_lock(self.base / 'out/scan.lock') as acquired:
            self.assertTrue(acquired)
            result = scan(self.config)
            self.assertEqual(result['status'], 'skipped')
        self.assertEqual(scan(self.config)['status'], 'complete')

    def test_scheduled_runner_logs_without_a_console(self):
        path = self.base / 'scheduled.json'
        path.write_text(json.dumps(self.config), encoding='utf-8')
        with patch.object(sys, 'stdout', None), patch.object(sys, 'stderr', None):
            code = scheduled_main(['--config', str(path)])
        self.assertEqual(code, 0)
        log = (self.base / 'out/scheduled-scan.log').read_text(encoding='utf-8')
        self.assertIn('"status": "complete"', log)
        self.assertIn('exit_code=0', log)

    def test_scheduled_runner_preserves_partial_collection_exit_code(self):
        path = self.base / 'scheduled.json'
        path.write_text(json.dumps(dict(self.config, roots=[str(self.base / 'missing')])), encoding='utf-8')
        code = scheduled_main(['--config', str(path)])
        self.assertEqual(code, 2)
        self.assertIn('exit_code=2', (self.base / 'out/scheduled-scan.log').read_text(encoding='utf-8'))

    def test_scheduled_runner_records_unexpected_errors(self):
        path = self.base / 'scheduled.json'
        path.write_text(json.dumps(self.config), encoding='utf-8')
        with patch('agent_inventory.scheduled.inventory_main', side_effect=RuntimeError('test failure')):
            self.assertEqual(scheduled_main(['--config', str(path)]), 1)
        self.assertIn('test failure', (self.base / 'out/scheduled-scan.log').read_text(encoding='utf-8'))


if __name__ == '__main__':
    unittest.main()
