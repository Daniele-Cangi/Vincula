# SPDX-FileCopyrightText: 2026 Daniele Cangi
# SPDX-License-Identifier: GPL-3.0-or-later

"""Reproduce the fixed three-part CPU workload on any Vincula node."""

import argparse
from hashlib import sha256
import json
from math import isqrt
from pathlib import Path
import platform
import sys
import time

BOUNDS = ((2, 1000000), (1000000, 2000000), (2000000, 3000000))
PASSES = 2
MAX_INPUT_BYTES = 1024 * 1024


def read_input(folder, index):
    with (folder / f'input-{index}.json').open('rb') as source:
        raw = source.read(MAX_INPUT_BYTES + 1)
    if len(raw) > MAX_INPUT_BYTES:
        raise ValueError('Benchmark input exceeds 1 MiB.')
    data = json.loads(raw)
    start, end = BOUNDS[index]
    expected = {'index': index, 'start': start, 'end': end, 'passes': PASSES}
    if (not isinstance(data, dict) or set(data) != set(expected) | {'padding'}
            or any(type(data[key]) is not int or data[key] != value for key, value in expected.items())
            or not isinstance(data['padding'], str)):
        raise ValueError('Input does not match the documented benchmark part.')
    return raw


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
    if len(args.parts) > len(BOUNDS) or len(set(args.parts)) != len(args.parts) or any(
            part not in range(len(BOUNDS)) for part in args.parts):
        parser.error('Choose each of the fixed parts 0, 1 and 2 at most once.')
    began = time.perf_counter()
    cpu_began = time.process_time()
    read_s = 0
    compute_s = 0
    parts = []
    inputs = []
    for index in args.parts:
        before_read = time.perf_counter()
        raw = read_input(args.data, index)
        start, end = BOUNDS[index]
        read_s += time.perf_counter() - before_read
        inputs.append({'index': index, 'bytes': len(raw), 'sha256': sha256(raw).hexdigest()})
        before_compute = time.perf_counter()
        counts = [count_primes(start, end) for _ in range(PASSES)]
        compute_s += time.perf_counter() - before_compute
        assert len(set(counts)) == 1
        parts.append({'index': index, 'start': start, 'end': end,
                      'passes': PASSES, 'primes': counts[0]})
    report = {
        'parts': parts, 'inputs': inputs, 'read_s': read_s, 'compute_s': compute_s,
        'cpu_s': time.process_time() - cpu_began, 'worker_wall_s': time.perf_counter() - began,
        'python': sys.version.split()[0], 'platform': platform.platform(),
    }
    print(json.dumps(report, separators=(',', ':')), flush=True)


if __name__ == '__main__':
    main()
