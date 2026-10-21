import datetime
import html
import json
import os
import sqlite3
import tempfile
from pathlib import Path


def now():
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def atomic_write(path, text):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(dir=str(path.parent), suffix='.tmp')
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as stream:
            stream.write(text)
        os.replace(temporary, str(path))
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


class Registry:
    def __init__(self, path):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(str(path), timeout=30)
        self.db.executescript('''
          CREATE TABLE IF NOT EXISTS cards(id TEXT PRIMARY KEY, revision TEXT, body TEXT, last_seen TEXT);
          CREATE TABLE IF NOT EXISTS revisions(id TEXT, revision TEXT, body TEXT, created_at TEXT, PRIMARY KEY(id,revision));
          CREATE TABLE IF NOT EXISTS approvals(id TEXT, revision TEXT, reviewer TEXT, approved_at TEXT, PRIMARY KEY(id,revision));
          CREATE TABLE IF NOT EXISTS scans(id INTEGER PRIMARY KEY, created_at TEXT, body TEXT);
          CREATE TABLE IF NOT EXISTS events(id TEXT PRIMARY KEY, agent_id TEXT, body TEXT);
        ''')

    def save(self, card):
        body, stamp = json.dumps(card, ensure_ascii=False), now()
        old = self.db.execute('SELECT revision FROM cards WHERE id=?', (card['id'],)).fetchone()
        with self.db:
            self.db.execute('INSERT OR IGNORE INTO revisions VALUES(?,?,?,?)', (card['id'], card['revision'], body, stamp))
            self.db.execute('INSERT OR REPLACE INTO cards VALUES(?,?,?,?)', (card['id'], card['revision'], body, stamp))
        return not old or old[0] != card['revision']

    def approve(self, agent_id, revision, reviewer):
        row = self.db.execute('SELECT revision,body FROM cards WHERE id=?', (agent_id,)).fetchone()
        if not row or row[0] != revision:
            raise ValueError('Unknown card or stale revision; review current card first')
        if json.loads(row[1])['warnings']:
            raise ValueError('Partial/invalid source collection: resolve card warnings before approval')
        with self.db:
            self.db.execute('INSERT OR REPLACE INTO approvals VALUES(?,?,?,?)', (agent_id, revision, reviewer, now()))

    def snapshot(self, discovery):
        with self.db:
            self.db.execute('INSERT INTO scans(created_at,body) VALUES(?,?)', (now(), json.dumps(discovery, ensure_ascii=False)))
            self.db.execute('DELETE FROM scans WHERE id NOT IN (SELECT id FROM scans ORDER BY id DESC LIMIT 500)')

    def known_roots(self):
        return sorted({json.loads(row[0])['root'] for row in self.db.execute('SELECT body FROM cards')})

    def cards(self):
        result = []
        for agent_id, revision, body, stamp in self.db.execute('SELECT * FROM cards ORDER BY id'):
            card = json.loads(body)
            approval = self.db.execute('SELECT reviewer,approved_at FROM approvals WHERE id=? AND revision=?', (agent_id, revision)).fetchone()
            published = self.db.execute('SELECT revision FROM approvals WHERE id=? ORDER BY approved_at DESC LIMIT 1', (agent_id,)).fetchone()
            card.update(last_seen=stamp, status='approved' if approval else 'draft',
                        approval={'reviewer': approval[0], 'at': approval[1]} if approval else None,
                        published_revision=published[0] if published else None)
            card['usage'] = self.usage(agent_id)
            result.append(card)
        return result

    def usage(self, agent_id):
        events = [json.loads(r[0]) for r in self.db.execute('SELECT body FROM events WHERE agent_id=?', (agent_id,))]
        if not events:
            return None
        success = sum(e['status'] == 'success' for e in events)
        users = {e['user_hash'] for e in events if e.get('user_hash')}
        tasks, skills, tools = {}, {}, {}
        for e in events:
            if e.get('task'):
                tasks[e['task']] = tasks.get(e['task'], 0) + 1
            for key, target in [('skills', skills), ('tools', tools)]:
                for name in e.get(key, []):
                    target[name] = target.get(name, 0) + 1
        return {'executions': len(events), 'users': len(users), 'success_rate': success / len(events),
                'mean_duration_ms': sum(e['duration_ms'] for e in events) / len(events),
                'from': min(e['timestamp'] for e in events), 'to': max(e['timestamp'] for e in events),
                'tasks': tasks, 'skills': skills, 'tools': tools,
                'scope': 'all imported events; tool lists represent individual calls'}

    def ingest(self, path):
        import math
        accepted, duplicate, errors = 0, 0, []
        with open(str(path), encoding='utf-8-sig') as stream, self.db:
            for number, line in enumerate(stream, 1):
                if not line.strip():
                    continue
                try:
                    e = json.loads(line)
                    if not isinstance(e, dict):
                        raise ValueError()
                    required = ('event_id', 'agent_id', 'timestamp', 'status', 'duration_ms')
                    if any(k not in e for k in required):
                        raise ValueError()
                    if any(not isinstance(e[k], str) or not e[k] for k in required[:4]):
                        raise ValueError()
                    if e['status'] not in ('success', 'failure') or isinstance(e['duration_ms'], bool):
                        raise ValueError()
                    if not isinstance(e['duration_ms'], (int, float)) or not math.isfinite(e['duration_ms']) or e['duration_ms'] < 0:
                        raise ValueError()
                    stamp = datetime.datetime.fromisoformat(e['timestamp'].replace('Z', '+00:00'))
                    if stamp.tzinfo is None:
                        raise ValueError()
                    e['timestamp'] = stamp.astimezone(datetime.timezone.utc).isoformat()
                    for k in ('skills', 'tools'):
                        if not isinstance(e.get(k, []), list) or any(not isinstance(v, str) for v in e.get(k, [])):
                            raise ValueError()
                    for k in ('user_hash', 'task'):
                        if k in e and not isinstance(e[k], str):
                            raise ValueError()
                    if not self.db.execute('SELECT 1 FROM cards WHERE id=?', (e['agent_id'],)).fetchone():
                        raise ValueError()
                    clean = {k: e[k] for k in required + ('user_hash', 'task', 'skills', 'tools') if k in e}
                    old = self.db.execute('SELECT body FROM events WHERE id=?', (e['event_id'],)).fetchone()
                    body = json.dumps(clean, sort_keys=True, ensure_ascii=False)
                    if old:
                        if old[0] != body:
                            raise ValueError()
                        duplicate += 1
                    else:
                        self.db.execute('INSERT INTO events VALUES(?,?,?)', (e['event_id'], e['agent_id'], body))
                        accepted += 1
                except (ValueError, TypeError, KeyError):
                    errors.append({'line': number, 'error': 'invalid, unknown agent, or conflicting event'})
        return {'accepted': accepted, 'duplicates': duplicate, 'errors': errors}

    def export(self, out):
        out = Path(out)
        cards = self.cards()
        latest = self.db.execute('SELECT created_at,body FROM scans ORDER BY id DESC LIMIT 1').fetchone()
        scan = json.loads(latest[1]) if latest else {}
        active = set(scan.get('observed_ids', []))
        for card in cards:
            card['observed_in_latest_scan'] = card['id'] in active
            card['runtime'] = [r for r in scan.get('runtime', []) if r['root'] == card['root']]
        payload = {'generated_at': now(), 'cards': cards, 'scan': scan}
        atomic_write(out / 'inventory.json', json.dumps(payload, ensure_ascii=False, indent=2))
        published = []
        for card in cards:
            if card['published_revision']:
                row = self.db.execute('SELECT body FROM revisions WHERE id=? AND revision=?', (card['id'], card['published_revision'])).fetchone()
                published.append(json.loads(row[0]))
        atomic_write(out / 'published.json', json.dumps(published, ensure_ascii=False, indent=2))
        panels = []
        for card in cards:
            esc = html.escape
            purpose = (card.get('purpose') or {}).get('value', '목적 미확인')
            overview = 'Owner: {} | Skill {} · MCP {} · RAG 후보 {} | 수집 경고 {}'.format(
                (card.get('owner') or {}).get('value', '미확인'), len(card['skills']), len(card['mcp']), len(card['rag']), len(card['warnings']))
            panels.append('<article><h2>{}</h2><p>{} · {} · {}</p><p>{}</p><p>{}</p><code>{}</code><details><summary>근거·구성·사용량 보기</summary><pre>{}</pre></details></article>'.format(
                esc(card['name']['value']), esc(card['status']), esc(card['classification']),
                '현재 스캔에서 확인' if card['observed_in_latest_scan'] else '이번 스캔에서 미확인',
                esc(purpose), esc(overview), esc(card['root']), esc(json.dumps(card, ensure_ascii=False, indent=2))))
        page = '''<!doctype html><html lang="ko"><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Agent Inventory</title><style>body{font:16px system-ui;margin:40px auto;max-width:1100px;padding:0 24px;background:#101722;color:#e5eaf3}h1{font-size:36px}article{background:#1b2638;border:1px solid #34435c;border-radius:14px;padding:24px;margin:20px 0}p{line-height:1.6;color:#bed0e5}code{overflow-wrap:anywhere}pre{white-space:pre-wrap;overflow-wrap:anywhere;font-size:13px}summary{cursor:pointer;margin-top:20px;color:#80cfef}input{padding:14px;width:90%;background:#1b2638;color:white;border:1px solid #526580;border-radius:8px}</style>
<h1>Agent Inventory</h1><p>로컬 소스 · 실행 프로세스 · 예약 작업에서 수집한 근거 기반 카탈로그</p>
<p>__COUNT__개 후보 · __STAMP__ · 새 스캔 결과는 페이지를 새로고침하여 확인</p>
<input id="q" placeholder="이름·경로·Skill·MCP 검색" aria-label="검색"><details><summary>수집 진단</summary><pre>__WARNINGS__</pre></details>
__PANELS__<script>document.getElementById('q').addEventListener('input',function(){let q=this.value.toLowerCase();document.querySelectorAll('article').forEach(a=>a.hidden=!a.textContent.toLowerCase().includes(q));});</script></html>'''
        page = page.replace('__COUNT__', str(len(cards))).replace('__STAMP__', html.escape(now())).replace('__WARNINGS__', html.escape(json.dumps(scan.get('warnings', []), ensure_ascii=False, indent=2))).replace('__PANELS__', ''.join(panels))
        atomic_write(out / 'index.html', page)
        return payload
