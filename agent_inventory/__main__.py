import argparse
import json
import math
import os
import sys
import time
from pathlib import Path

from .analyzer import analyze
from .discovery import discover, diagnostic, excluded_path
from .registry import Registry, atomic_write, now
from .locking import scan_lock


def load_config(path):
    path = Path(path).resolve()
    with path.open(encoding='utf-8-sig') as stream:
        config = json.load(stream)
    for key in ('roots', 'exclude'):
        config[key] = [str((path.parent / Path(p).expanduser()).resolve()) for p in config.get(key, [])]
    config['output'] = str((path.parent / config.get('output', '.inventory')).resolve())
    config['exclude'].append(config['output'])
    for key in ('discovery_depth', 'source_depth', 'max_files', 'max_file_bytes'):
        if key in config and (type(config[key]) is not int or config[key] < 1):
            raise ValueError(key + ' must be a positive integer')
    config['event_files'] = [str((path.parent / p).resolve()) for p in config.get('event_files', [])]
    return config


def scan(config):
    with scan_lock(Path(config['output']) / 'scan.lock') as acquired:
        if not acquired:
            return {'status': 'skipped', 'reason': 'another scan is running', 'exit_code': 0}
        return scan_unlocked(config)


def scan_unlocked(config):
    registry = Registry(Path(config['output']) / 'registry.sqlite3')
    changed, observed, partial_cards = 0, [], 0
    identities = {}
    try:
        candidates, runtime, issues = discover(config)
        # Persist discovery independently of whether a process happens to be alive.
        if config.get('track_discovered', True):
            for root in registry.known_roots():
                if excluded_path(root, config.get('exclude', [])):
                    continue
                if Path(root).is_dir():
                    candidates.setdefault(root, [{'kind': 'previously-discovered', 'source': root}])
                else:
                    diagnostic(issues, 'tracked_root_missing', 'Previously discovered root unavailable: ' + root, 'warning', 'filesystem')
        for root in sorted(candidates):
            try:
                card = analyze(root, config)
                if card['id'] in identities:
                    diagnostic(issues, 'duplicate_id', 'Duplicate declared ID: {} and {}'.format(identities[card['id']], root), 'error', 'analyzer')
                    continue
                identities[card['id']] = root
                changed += int(registry.save(card))
                observed.append(card['id'])
                partial_cards += bool(card['warnings'])
            except (OSError, ValueError) as exc:
                diagnostic(issues, 'analysis_failed', 'Analysis failed for {}: {}'.format(root, type(exc).__name__), 'error', 'analyzer')
        # Configured normalized logs are idempotently imported on every pass.
        for path in config.get('event_files', []):
            try:
                result = registry.ingest(path)
                if result['errors']:
                    diagnostic(issues, 'events_invalid', 'Invalid event rows in {}: {}'.format(path, len(result['errors'])), 'error', 'events')
            except OSError:
                diagnostic(issues, 'events_unavailable', 'Event file unavailable: ' + path, 'error', 'events')
        incomplete = partial_cards or any(i['severity'] in ('error', 'warning') for i in issues)
        status = 'partial' if incomplete else 'complete'
        if not observed and any(i['severity'] == 'error' for i in issues):
            status = 'failed'
        warnings = [i['message'] for i in issues]
        registry.snapshot({'runtime': runtime, 'warnings': warnings, 'issues': issues, 'status': status,
                           'observed_ids': observed, 'discovery': candidates})
        registry.export(config['output'])
        result = {'status': status, 'exit_code': 0 if status == 'complete' else 2,
                  'finished_at': now(), 'observed': len(observed), 'changed': changed,
                  'cards_with_warnings': partial_cards, 'warnings': warnings, 'issues': issues,
                  'report': str(Path(config['output']) / 'index.html')}
        atomic_write(Path(config['output']) / 'last-scan.json', json.dumps(result, ensure_ascii=False, indent=2))
        return result
    finally:
        registry.db.close()


def calculate_kpi(data):
    if data.get('direction') not in ('lower_is_better', 'higher_is_better'):
        raise ValueError('KPI direction must be explicit')
    for key in ('name', 'unit', 'baseline_source', 'after_source', 'baseline_period', 'after_period', 'cohort', 'approved_by'):
        if not isinstance(data.get(key), str) or not data[key].strip():
            raise ValueError('Missing KPI definition: ' + key)
    before, after = data.get('before'), data.get('after')
    if any(isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) or v < 0 for v in (before, after)):
        raise ValueError('KPI values must be finite nonnegative numbers')
    if before == 0:
        raise ValueError('Cannot calculate relative improvement from a zero baseline')
    delta = before - after if data['direction'] == 'lower_is_better' else after - before
    return dict(data, improvement_percent=round(delta / before * 100, 4),
                formula='(before-after)/before*100' if data['direction'] == 'lower_is_better' else '(after-before)/before*100',
                verification='calculated from supplied aggregates; comparability and approval are caller declarations')


def main(argv=None):
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, 'reconfigure'):
            stream.reconfigure(encoding='utf-8')
    parser = argparse.ArgumentParser(description='Local Agent Inventory: inspect sources without executing them')
    parser.add_argument('--config', default='inventory.config.json')
    sub = parser.add_subparsers(dest='command')
    init = sub.add_parser('init', help='write config; no system changes')
    init.add_argument('--root', action='append', default=[])
    init.add_argument('--no-host', action='store_true')
    sub.add_parser('scan', help='one scan; suitable for Task Scheduler/cron/CI')
    watch = sub.add_parser('watch', help='repeat discovery and hash scans')
    watch.add_argument('--interval', type=float, default=30)
    watch.add_argument('--cycles', type=int, default=0, help='0 runs until Ctrl+C')
    sub.add_parser('list')
    sub.add_parser('export')
    approve = sub.add_parser('approve')
    approve.add_argument('id')
    approve.add_argument('--revision', required=True)
    approve.add_argument('--reviewer', required=True)
    ingest = sub.add_parser('ingest')
    ingest.add_argument('path')
    kpi = sub.add_parser('kpi')
    kpi.add_argument('path')
    args = parser.parse_args(argv)
    if not args.command:
        parser.print_help()
        return 0
    try:
        if args.command == 'init':
            if Path(args.config).exists():
                raise ValueError('Config already exists; edit it directly')
            config = {'roots': [str(Path(p).resolve()) for p in args.root], 'discover_host': not args.no_host,
                      'output': '.inventory', 'exclude': [], 'discovery_depth': 4,
                      'source_depth': 12, 'max_files': 3000, 'max_file_bytes': 1000000, 'event_files': []}
            atomic_write(args.config, json.dumps(config, ensure_ascii=False, indent=2))
            print('Created ' + args.config)
            return 0
        if args.command == 'kpi':
            with open(args.path, encoding='utf-8-sig') as stream:
                print(json.dumps(calculate_kpi(json.load(stream)), ensure_ascii=False, indent=2))
            return 0
        config = load_config(args.config)
        if args.command == 'scan':
            result = scan(config)
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return result['exit_code']
        if args.command == 'watch':
            if not math.isfinite(args.interval) or args.interval < 1 or args.cycles < 0:
                raise ValueError('interval >= 1 and cycles >= 0 required')
            cycle = 0
            while True:
                print(json.dumps(scan(config), ensure_ascii=False), flush=True)
                cycle += 1
                if args.cycles and cycle >= args.cycles:
                    break
                time.sleep(args.interval)
            return 0
        registry = Registry(Path(config['output']) / 'registry.sqlite3')
        try:
            if args.command == 'list':
                print(json.dumps([{'id': c['id'], 'name': c['name']['value'], 'revision': c['revision'], 'status': c['status']} for c in registry.cards()], ensure_ascii=False, indent=2))
            elif args.command == 'approve':
                registry.approve(args.id, args.revision, args.reviewer)
                registry.export(config['output'])
                print('Approved ' + args.id)
            elif args.command == 'export':
                registry.export(config['output'])
                print('Exported to ' + config['output'])
            elif args.command == 'ingest':
                result = registry.ingest(args.path)
                registry.export(config['output'])
                print(json.dumps(result, ensure_ascii=False, indent=2))
                return 1 if result['errors'] else 0
        finally:
            registry.db.close()
        return 0
    except KeyboardInterrupt:
        print('Stopped')
        return 0
    except (OSError, ValueError, TypeError) as exc:
        print('Error: ' + str(exc), file=sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(main())
