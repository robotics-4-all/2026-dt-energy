#!/usr/bin/env python3
"""detector_eval.py -- how well does the twin's Modified Z-score detector find KNOWN injected anomalies?

The detector below is a line-by-line copy of the logic in templates/analytics_consumer.py.jinja
(rolling window of 30 previous values, median/MAD with factor 1.4826, MAD floor 0.5 % of the median,
|z| > 3.5 critical, persistence of 2 consecutive samples). Keep the two in sync. In the twin it is
applied to voltage; here it is applied to the same kind of series (a positive, slowly varying value).

Series
  real : ENTSO-E Actual Total Load (Greece), the series that calibrated the twin
  twin : the output of the generated edge simulator (noise + the daily profile of the .seg)

Anomalies (relative to the value, injected by this script, so the ground truth is certain)
  spike : one sample multiplied by (1 + m)
  shift : 8 samples multiplied by (1 + m)
  drift : 16 samples, factor rising linearly from 1 to (1 + m)
Events are 70+ samples apart; each (type, magnitude) is repeated at 8 positions.
An event counts as DETECTED when an alarm that the clean series does not have appears from its first sample
until 4 samples after its last sample, for p1 (one sample over the limit) and p2 (two consecutive, the alert rule). Delay = samples from the start of the event to the first p2.
False alarms are counted on the CLEAN series (no injection), as p2 samples per 1000 samples.

Two ways to feed the detector
  raw       : the series as it is (the detector as built)
  deseason  : the series divided by the daily curve of the .seg, so the daily swing is removed first

Usage:  python tools/detector_eval.py --seg tools/examples/generated/entsoe_load_gr.seg \\
            --xml data/entsoe_actual_total_load_GR_2026-09-25_to_10-01.xml --twin-dir entsoe_load_gr_twin
"""
import argparse
import json
import os
import re
import statistics
import sys
import types
from collections import deque

import numpy as np
import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

# ---- detector constants (copy of templates/analytics_consumer.py.jinja)
WINDOW, MIN_SAMPLES, MIN_STD_FRACTION, MAD_FACTOR = 30, 10, 0.005, 1.4826
WARNING, CRITICAL, PERSISTENCE = 2.0, 3.5, 2

SPIKE_LEN, SHIFT_LEN, DRIFT_LEN = 1, 8, 16
MAGNITUDES = (0.03, 0.05, 0.10, 0.20)
POSITIONS = 8
SPACING = 70
TAIL = 4


class Detector:
    """One node's detector, same order of operations as evaluate(): test first, then append to the window."""

    def __init__(self):
        self.window = deque(maxlen=WINDOW)
        self.critical_streak = 0

    def step(self, value):
        z = p1 = p2 = None
        if value is not None and value > 0:
            if len(self.window) >= MIN_SAMPLES:
                med = statistics.median(self.window)
                mad = statistics.median([abs(x - med) for x in self.window]) * MAD_FACTOR
                mad = max(mad, abs(med) * MIN_STD_FRACTION)
                if mad > 0:
                    z = abs(value - med) / mad
                    self.critical_streak = self.critical_streak + 1 if z > CRITICAL else 0
                    p1 = z > CRITICAL
                    p2 = self.critical_streak >= PERSISTENCE
            self.window.append(value)
        return z, bool(p1), bool(p2)


def run_detector(series):
    d = Detector()
    out = [d.step(float(v)) for v in series]
    return np.array([o[1] for o in out]), np.array([o[2] for o in out])


def inject(series, kind, start, mag):
    x = np.array(series, float)
    if kind == 'spike':
        x[start] *= 1 + mag
        end = start + SPIKE_LEN
    elif kind == 'shift':
        x[start:start + SHIFT_LEN] *= 1 + mag
        end = start + SHIFT_LEN
    else:
        x[start:start + DRIFT_LEN] *= 1 + mag * np.arange(1, DRIFT_LEN + 1) / DRIFT_LEN
        end = start + DRIFT_LEN
    return x, end


def positions(n, length, rng):
    """POSITIONS start indexes at least SPACING apart, after the detector has filled its window."""
    lo, hi = WINDOW + 10, n - length - TAIL - SPACING // 3 - 5   # room for the random shift below
    slots = np.arange(lo, hi, SPACING)
    if len(slots) < POSITIONS:
        raise ValueError(f'series too short: {n} samples give {len(slots)} slots, need {POSITIONS}')
    chosen = rng.choice(len(slots), POSITIONS, replace=False)
    return sorted(int(slots[i] + rng.integers(0, SPACING // 3)) for i in chosen)


def evaluate(series, label, rng_seed=7):
    """series: the signal the detector sees (positive). Returns a dict of results."""
    series = np.asarray(series, float)
    p1_clean, p2_clean = run_detector(series)
    res = {'label': label, 'n': len(series),
           'false_alarms_p2_per_1000': float(p2_clean.sum() / len(series) * 1000),
           'false_alarms_p1_per_1000': float(p1_clean.sum() / len(series) * 1000),
           'events': {}}
    for kind, length in (('spike', SPIKE_LEN), ('shift', SHIFT_LEN), ('drift', DRIFT_LEN)):
        for mag in MAGNITUDES:
            rng = np.random.default_rng(rng_seed + int(mag * 1000))
            hits_p1 = hits_p2 = 0
            delays_p1, delays_p2 = [], []
            for start in positions(len(series), length, rng):
                x, end = inject(series, kind, start, mag)
                p1, p2 = run_detector(x)
                win = slice(start, end + TAIL)
                # only alarms CAUSED by the injection count: an alarm the clean series already has there is not a detection
                new1 = (p1 & ~p1_clean)[win]
                new2 = (p2 & ~p2_clean)[win]
                if new1.any():
                    hits_p1 += 1
                    delays_p1.append(int(np.argmax(new1)))
                if new2.any():
                    hits_p2 += 1
                    delays_p2.append(int(np.argmax(new2)))
            res['events'][f'{kind}@{int(mag * 100)}%'] = {
                'detected_p1': hits_p1, 'detected_p2': hits_p2, 'of': POSITIONS,
                'median_delay_p1': (float(np.median(delays_p1)) if delays_p1 else None),
                'median_delay_p2': (float(np.median(delays_p2)) if delays_p2 else None)}
    return res


def read_profile(seg_path):
    m = re.search(r'dailyProfile:\s*"([^"]+)"', open(seg_path, encoding='utf-8').read())
    prof = np.array([float(x) for x in m.group(1).split(',')])
    assert len(prof) == 24
    return prof


def curve_at_hours(profile, hours):
    pos = np.asarray(hours, float) % 24
    i = pos.astype(int)
    f = pos - i
    return profile[i] * (1 - f) + profile[(i + 1) % 24] * f


def real_series(xml_path, seg_path):
    import dataset_to_seg as d
    df = d.load_entsoe_xml(xml_path, {'businessType': 'A04'})
    ts = pd.to_datetime(df['timestamp'])
    ts = ts.dt.tz_localize('UTC') if ts.dt.tz is None else ts
    local = ts.dt.tz_convert('Europe/Athens')
    hours = (local.dt.hour + local.dt.minute / 60).to_numpy()
    return df['value'].to_numpy(float), hours


def twin_series(twin_dir, steps, seed=1):
    import random
    for name in ('paho', 'paho.mqtt', 'paho.mqtt.client'):
        sys.modules.setdefault(name, types.ModuleType(name))
    sys.modules['paho.mqtt.client'].Client = object
    sys.modules['paho.mqtt'].client = sys.modules['paho.mqtt.client']
    sys.path.insert(0, os.path.abspath(twin_dir))
    import edge_simulator as es
    np.random.seed(seed)
    random.seed(seed)
    grid = es.build_topology()
    node = next(n for n in grid.nodes.values() if n['type'] in ('Industrial', 'Residential') and n['daily_profile'])
    out = []
    for step in range(steps):
        es.process_telemetry_physics(grid, step, {})
        out.append(node['sim_active_power'])
    return np.array(out), np.arange(steps) / es.STEPS_PER_HOUR


def print_table(results):
    kinds = [f'{k}@{int(m * 100)}%' for k in ('spike', 'shift', 'drift') for m in MAGNITUDES]
    for rule, title in (('p1', 'p1: ONE sample above the critical limit (a spike can only be caught by this)'),
                        ('p2', 'p2: TWO consecutive samples above the critical limit (the rule that raises the alert in the twin)')):
        print(title)
        print(f"{'':34s}" + ''.join(f'{k:>12s}' for k in kinds))
        for r in results:
            cells = []
            for k in kinds:
                e = r['events'][k]
                d = e[f'median_delay_{rule}']
                cells.append(f"{e['detected_' + rule]}/{e['of']}" + (f" ({d:.0f})" if d is not None else ''))
            print(f"{r['label'] + ' (n=' + str(r['n']) + ')':34s}" + ''.join(f'{c:>12s}' for c in cells))
        print()
    for r in results:
        print(f"false alarms, clean series, {r['label']:30s}: p2 {r['false_alarms_p2_per_1000']:.1f} / 1000 samples,"
              f" p1 {r['false_alarms_p1_per_1000']:.1f} / 1000")
    print('\ncells: injected events detected / 8 positions (median delay in samples). Only alarms the injection added count.')


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--seg', required=True)
    ap.add_argument('--xml', required=True)
    ap.add_argument('--twin-dir', required=True, help='folder written by generate_twin.py for the same .seg')
    ap.add_argument('--twin-steps', type=int, default=1200, help='simulation steps (120 steps = 1 simulated day)')
    ap.add_argument('--json', default=None)
    args = ap.parse_args()

    profile = read_profile(args.seg)
    real, real_hours = real_series(args.xml, args.seg)
    twin, twin_hours = twin_series(args.twin_dir, args.twin_steps)

    results = []
    for name, x, hrs in (('real ENTSO-E', real, real_hours), ('twin', twin, twin_hours)):
        results.append(evaluate(x, f'{name}, raw'))
        flat = x / curve_at_hours(profile, hrs) * np.mean(x)
        results.append(evaluate(flat, f'{name}, deseason'))
    print_table(results)
    if args.json:
        json.dump(results, open(args.json, 'w', encoding='utf-8'), indent=1)


if __name__ == '__main__':
    main()
