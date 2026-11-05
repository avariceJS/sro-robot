#!/usr/bin/env python3
"""Read-only snapshot of one llama-server process and host swap state.

This script does not connect to the server or issue inference requests. Run it
once immediately before and after each controlled request. The after snapshot
includes vmmap's process-lifetime physical-footprint peak; pass the request
elapsed time from the caller/harness for attribution to that request.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import re
import subprocess
import sys
import time


def run(args: list[str]) -> str:
    return subprocess.run(args, check=True, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE).stdout.strip()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--pid', required=True, type=int)
    parser.add_argument('--label', required=True, choices=('before', 'after'))
    parser.add_argument('--active-request-elapsed-seconds', type=float)
    parser.add_argument('--output')
    args = parser.parse_args()

    epoch = time.time()
    ps = run(['ps', '-p', str(args.pid), '-o', 'pid=,rss=,etime='])
    fields = ps.split()
    if len(fields) != 3 or int(fields[0]) != args.pid:
        raise RuntimeError(f'could not parse ps output: {ps!r}')
    vmmap = run(['vmmap', '-summary', str(args.pid)])
    footprint_match = re.search(r'^Physical footprint:\s+([0-9.]+\s*[KMG])', vmmap, re.M)
    peak_match = re.search(r'^Physical footprint \(peak\):\s+([0-9.]+\s*[KMG])', vmmap, re.M)
    swap = run(['sysctl', 'vm.swapusage'])
    used_match = re.search(r'used\s*=\s*([0-9.]+)M', swap)
    free_match = re.search(r'free\s*=\s*([0-9.]+)M', swap)
    if not footprint_match or not peak_match or not used_match or not free_match:
        raise RuntimeError('could not parse vmmap/swap output')
    report = {
        'label': args.label,
        'captured_at_utc': dt.datetime.fromtimestamp(epoch, dt.timezone.utc).isoformat(),
        'captured_at_epoch': epoch,
        'pid': args.pid,
        'process_elapsed': fields[2],
        'rss_kib': int(fields[1]),
        'vmmap_physical_footprint': footprint_match.group(1),
        'vmmap_physical_footprint_peak': peak_match.group(1),
        'system_swap_used_mib': float(used_match.group(1)),
        'system_swap_free_mib': float(free_match.group(1)),
        'system_swap_raw': swap,
        'active_request_elapsed_seconds': args.active_request_elapsed_seconds,
        'source_commands': ['ps -p PID -o pid=,rss=,etime=', 'vmmap -summary PID', 'sysctl vm.swapusage'],
    }
    rendered = json.dumps(report, indent=2) + '\n'
    if args.output:
        with open(args.output, 'w', encoding='utf-8') as f:
            f.write(rendered)
    else:
        sys.stdout.write(rendered)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
