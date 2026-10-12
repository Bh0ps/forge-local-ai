"""Run one deterministic backend CI shard; regular pytest is unaffected."""
import argparse
from hashlib import sha256
import json
from pathlib import Path

import pytest


class BackendShard:
    def __init__(self, shard, shards, report=None):
        self.shard = shard
        self.shards = shards
        self.report = report

    @pytest.hookimpl(trylast=True)
    def pytest_collection_modifyitems(self, config, items):
        collected = len(items)
        selected, deselected = [], []
        for item in items:
            bucket = int.from_bytes(sha256(item.nodeid.encode('utf-8')).digest(), 'big') % self.shards
            (selected if bucket == self.shard else deselected).append(item)
        items[:] = selected
        config.hook.pytest_deselected(items=deselected)
        if self.report is not None:
            self.report.parent.mkdir(parents=True, exist_ok=True)
            self.report.write_text(json.dumps({
                'shard': self.shard,
                'shards': self.shards,
                'collected': collected,
                'selected': len(selected),
                'nodeids': [item.nodeid for item in selected],
            }, indent=2) + '\n', encoding='utf-8', newline='\n')


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--shard', type=int, required=True, help='Zero-based shard index.')
    parser.add_argument('--shards', type=int, default=4, help='Shard count (1–64; default: 4).')
    parser.add_argument('--report', type=Path, help='Optional JSON report of selected test node IDs.')
    args, pytest_args = parser.parse_known_args(argv)
    if not 1 <= args.shards <= 64 or not 0 <= args.shard < args.shards:
        parser.error('--shards must be 1–64 and --shard must be between 0 and --shards minus 1')
    return pytest.main(pytest_args, plugins=[BackendShard(args.shard, args.shards, args.report)])


if __name__ == '__main__':
    raise SystemExit(main())
