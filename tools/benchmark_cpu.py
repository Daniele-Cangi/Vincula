# SPDX-FileCopyrightText: 2026 Daniele Cangi
# SPDX-License-Identifier: GPL-3.0-or-later

"""Deterministic single-process CPU fixture shared unchanged by every node."""

import argparse
from hashlib import sha256
import json
from math import isqrt
from pathlib import Path
import platform
import sys
import time


def count_primes(start, end):
    return sum(
        1 for candidate in range(start, end)
        if (candidate == 2 or candidate % 2)
        and all(candidate % divisor for divisor in range(3, isqrt(candidate) + 1, 2))
    )


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--data', type=Path, required=True)
    parser.add_argument('--parts', type=int, nargs='+', required=True)
    args = parser.parse_args()
    began = time.perf_counter()
    cpu_began = time.process_time()
    read_s = 0
    compute_s = 0
    parts = []
    inputs = []
    for index in args.parts:
        before_read = time.perf_counter()
        raw = (args.data / f'input-{index}.json').read_bytes()
        data = json.loads(raw)
        read_s += time.perf_counter() - before_read
        assert data['index'] == index
        inputs.append({'index': index, 'bytes': len(raw), 'sha256': sha256(raw).hexdigest()})
        before_compute = time.perf_counter()
        counts = [count_primes(data['start'], data['end']) for _ in range(data['passes'])]
        compute_s += time.perf_counter() - before_compute
        assert len(set(counts)) == 1
        parts.append({'index': index, 'start': data['start'], 'end': data['end'],
                      'passes': data['passes'], 'primes': counts[0]})
    report = {
        'parts': parts, 'inputs': inputs, 'read_s': read_s, 'compute_s': compute_s,
        'cpu_s': time.process_time() - cpu_began, 'worker_wall_s': time.perf_counter() - began,
        'python': sys.version.split()[0], 'platform': platform.platform(),
    }
    print(json.dumps(report, separators=(',', ':')), flush=True)


if __name__ == '__main__':
    main()
