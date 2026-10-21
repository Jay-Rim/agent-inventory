"""Windowless scheduled entrypoint with a UTF-8 log, independent of shell policy."""
import argparse
import contextlib
import traceback
from pathlib import Path

from .__main__ import load_config, main as inventory_main
from .registry import now


def main(argv=None):
    parser = argparse.ArgumentParser(description='Run one scheduled inventory scan')
    parser.add_argument('--config', default='inventory.config.json')
    args = parser.parse_args(argv)
    config_path = Path(args.config).resolve()
    try:
        output = Path(load_config(config_path)['output'])
    except (OSError, ValueError, TypeError):
        output = config_path.parent / '.inventory'
    output.mkdir(parents=True, exist_ok=True)
    with (output / 'scheduled-scan.log').open('w', encoding='utf-8') as log:
        with contextlib.redirect_stdout(log), contextlib.redirect_stderr(log):
            print('Started: ' + now(), flush=True)
            try:
                code = inventory_main(['--config', str(config_path), 'scan'])
            except Exception:
                traceback.print_exc()
                code = 1
            print('Finished: {} exit_code={}'.format(now(), code), flush=True)
    return code


if __name__ == '__main__':
    raise SystemExit(main())
