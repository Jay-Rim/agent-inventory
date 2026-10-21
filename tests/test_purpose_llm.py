import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from agent_inventory.analyzer import analyze
from agent_inventory.purpose_llm import documents, request_summary, validate


class PurposeLLMTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name).resolve()
        self.root = self.base / 'project'
        self.root.mkdir()
        self.doc = self.root / 'README.md'
        self.doc.write_text('# Setup\n\nInstall first.\n\n## Details\nChecks vacancies and sends alerts.', encoding='utf-8')
        self.config = {'exclude': [], 'output': str(self.base / 'out'),
                       'purpose_llm': {'enabled': True, 'endpoint': 'http://localhost:1234/v1/chat/completions', 'model': 'test'}}
        self.result = {'purpose': '빈자리를 확인하고 알림을 보냅니다.', 'evidence': [
            {'source': 'README.md', 'quote': 'Checks vacancies and sends alerts.'}]}

    def tearDown(self):
        self.temp.cleanup()

    def test_whole_document_and_cache_invalidation(self):
        with patch('agent_inventory.purpose_llm.request_summary', return_value=self.result) as call:
            first = analyze(self.root, self.config)
            self.assertIn('## Details', call.call_args[0][1]['README.md'])
            self.assertEqual(first['purpose']['value'], self.result['purpose'])
            self.assertEqual(first['purpose']['evidence'][0]['line'], 6)
            self.assertEqual(analyze(self.root, self.config)['revision'], first['revision'])
            self.assertEqual(call.call_count, 1)
            (self.root / 'main.py').write_text('print(1)', encoding='utf-8')
            analyze(self.root, self.config)
            self.assertEqual(call.call_count, 1)
            self.doc.write_text(self.doc.read_text(encoding='utf-8') + '\nMore details.', encoding='utf-8')
            analyze(self.root, self.config)
            self.assertEqual(call.call_count, 2)
            self.config['purpose_llm']['model'] = 'different'
            analyze(self.root, self.config)
            self.assertEqual(call.call_count, 3)

    def test_bad_evidence_or_provider_failure_keeps_excerpt(self):
        for result in [{'purpose': 'Invented', 'evidence': [{'source': 'README.md', 'quote': 'not in file'}]},
                       {'purpose': None, 'evidence': []}]:
            with patch('agent_inventory.purpose_llm.request_summary', return_value=result):
                card = analyze(self.root, self.config)
                self.assertNotEqual(card['purpose']['method'], 'llm-markdown')
                self.assertTrue(card['warnings'])
        # A separate model key avoids the cached insufficient-evidence result.
        self.config['purpose_llm']['model'] = 'unavailable'
        with patch('agent_inventory.purpose_llm.request_summary', side_effect=OSError('SECRET credential')):
            card = analyze(self.root, self.config)
            self.assertEqual(card['purpose_analysis']['status'], 'failed')
            self.assertNotIn('SECRET', json.dumps(card))

    def test_disabled_and_declared_purpose_never_call_provider(self):
        with patch('agent_inventory.purpose_llm.request_summary') as call:
            analyze(self.root, dict(self.config, purpose_llm={'enabled': False}))
            (self.root / 'agent-card.json').write_text('{"purpose":"Reviewed"}', encoding='utf-8')
            card = analyze(self.root, self.config)
            self.assertEqual(card['purpose']['value'], 'Reviewed')
            call.assert_not_called()

    def test_over_limit_is_explicit_not_first_page_summary(self):
        self.doc.write_text('x' * 120001, encoding='utf-8')
        with patch('agent_inventory.purpose_llm.request_summary') as call:
            card = analyze(self.root, self.config)
            call.assert_not_called()
            self.assertEqual(card['purpose_analysis']['status'], 'failed')
            self.assertTrue(card['warnings'])

    def test_redaction_excludes_cache_and_credentials(self):
        docs = documents({'README.md': b'api_key=VERYSECRET\nhttps://user:pass@example.com/private?token=abc\nPurpose',
                          '.pytest_cache/README.md': b'cache'})
        self.assertNotIn('.pytest_cache/README.md', docs)
        self.assertNotIn('VERYSECRET', docs['README.md'])
        self.assertNotIn('user:pass', docs['README.md'])

    def test_http_transport_format_and_incomplete_response(self):
        response = {'choices': [{'finish_reason': 'stop', 'message': {'content': json.dumps(self.result)}}]}
        with patch('agent_inventory.purpose_llm.build_opener') as opener:
            stream = opener.return_value.open.return_value.__enter__.return_value
            stream.read.return_value = json.dumps(response).encode('utf-8')
            self.assertEqual(request_summary(self.config['purpose_llm'], {'README.md': 'all text'}), self.result)
            req = opener.return_value.open.call_args[0][0]
            body = json.loads(req.data)
            self.assertEqual(body['messages'][1]['content'], '{"README.md": "all text"}')
            self.assertNotIn('tools', body)
            response['choices'][0]['finish_reason'] = 'length'
            stream.read.return_value = json.dumps(response).encode('utf-8')
            with self.assertRaises(ValueError):
                request_summary(self.config['purpose_llm'], {})

    def test_nonlocal_plaintext_endpoint_rejected(self):
        with self.assertRaises(ValueError):
            request_summary({'endpoint': 'http://example.com/v1/chat/completions', 'model': 'test'}, {})
