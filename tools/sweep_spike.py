#!/usr/bin/env python3
"""sweep_spike.py -- smallest spike the twin's detector can find ("minimum detectable anomaly").

Method (Nixtla blog, 26/08/2025: "Performance Evaluation of Anomaly Detection through Synthetic Anomalies"):
  fix a size, inject the SAME anomaly at several locations of the series, count how many are detected
  (accuracy = detected / injected), then reduce the size until accuracy falls below a chosen level.
  The smallest size that still reaches the level is the minimum detectable anomaly.

What is OUR choice (not from the blog)
  * one run = 60 calm samples, ONE spike, 60 samples after (cold detector each run, like a restarted consumer)
  * size = fraction of the value of the sample itself (blog: fraction of a local mean); upward only (as in the blog)
  * detected = rule p1 (one sample over the critical limit) fires AT the spike sample
  * the first MIN_SAMPLES samples are the detector's warm-up and are not judged
  * the detector is the offline copy of the twin's (tools/detector_eval.py), applied after dividing by the daily curve
  * locations: evenly spread over the series; sizes: see --sizes; level: --level

  python tools/sweep_spike.py --seg tools/examples/generated/entsoe_load_gr.seg \\
      --xml data/entsoe_actual_total_load_GR_2026-09-25_to_10-01.xml
"""
import argparse
import json
import os
import sys

import numpy as np
import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import dataset_to_seg as dts
import detector_eval as de

CALM, AFTER = 60, 60


def load(xml, seg, tz='Europe/Athens'):
    df = dts.load_entsoe_xml(xml, {'businessType': 'A04'})
    loc = pd.to_datetime(df['timestamp'], utc=True).dt.tz_convert(tz)
    hours = (loc.dt.hour + loc.dt.minute / 60).to_numpy(float)
    return df['value'].to_numpy(float), hours, np.array(de.read_profile(seg))


def one_run(x, begin, size):
    """x: deseasoned series. Returns the p1 and p2 arrays of one cold-start run."""
    seg = x[begin:begin + CALM + 1 + AFTER].copy()
    seg[CALM] *= 1 + size
    return de.run_detector(seg)


def sweep(values, hours, profile, sizes, n_pos):
    x = values / de.curve_at_hours(profile, hours)
    last = len(x) - (CALM + 1 + AFTER)
    begins = np.unique(np.linspace(0, last, n_pos).round().astype(int))
    skip = np.arange(CALM + 1 + AFTER) < de.MIN_SAMPLES
    spike = np.arange(CALM + 1 + AFTER) == CALM
    rows = []
    for s in sizes:
        tp = fp = fn = tn = 0
        fp_calm = fp2 = fp2_calm = 0
        for b in begins:
            p, p2 = one_run(x, b, s)
            fp2 += int((p2 & ~spike & ~skip).sum())
            fp2_calm += int((p2 & ~spike & ~skip & (np.arange(len(p2)) < CALM)).sum())
            ok = ~skip
            tp += int((p & spike & ok).sum()); fn += int((~p & spike & ok).sum())
            fp += int((p & ~spike & ok).sum()); tn += int((~p & ~spike & ok).sum())
            fp_calm += int((p & ~spike & ok & (np.arange(len(p)) < CALM)).sum())
        n = len(begins)
        prec = tp / (tp + fp) if tp + fp else None
        rec = tp / (tp + fn) if tp + fn else None
        f1 = 2 * prec * rec / (prec + rec) if prec and rec else (0.0 if prec is not None and rec is not None else None)
        rows.append({'size': s, 'runs': n, 'detected': tp, 'accuracy': tp / n, 'TP': tp, 'FP': fp, 'FN': fn,
                     'TN': tn, 'precision': prec, 'recall': rec, 'f1': f1, 'fp_calm': fp_calm,
                     'p2_fp': fp2, 'p2_fp_calm': fp2_calm})
    return rows, begins


def per_location(values, hours, profile, n_pos):
    """False alarms of the calm part (no spike), one row per location: how the totals p1 / p2 per 1000 are made up."""
    x = values / de.curve_at_hours(profile, hours)
    last = len(x) - (CALM + 1 + AFTER)
    begins = np.unique(np.linspace(0, last, n_pos).round().astype(int))
    out = []
    for b in begins:
        p1, p2 = one_run(x, b, 0.0)
        out.append({'begin': int(b), 'hour': float(hours[b]),
                    'p1': int(p1[de.MIN_SAMPLES:CALM].sum()), 'p2': int(p2[de.MIN_SAMPLES:CALM].sum())})
    return out


def minimum_detectable(rows, level):
    """Reduce the size from the largest one; stop at the first size below the level (blog's loop)."""
    best = None
    for r in sorted(rows, key=lambda r: -r['size']):
        if r['accuracy'] >= level:
            best = r['size']
        else:
            break
    return best


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--seg', required=True)
    ap.add_argument('--xml', required=True)
    ap.add_argument('--sizes', default='0,0.01,0.02,0.03,0.04,0.05,0.06,0.08,0.10,0.15,0.20,0.30')
    ap.add_argument('--positions', type=int, default=20)
    ap.add_argument('--level', type=float, default=0.5)
    ap.add_argument('--per-location', action='store_true', help='also print the false alarms of the calm part per location')
    ap.add_argument('--json', default=None)
    a = ap.parse_args()
    values, hours, profile = load(a.xml, a.seg)
    sizes = [float(s) for s in a.sizes.split(',')]
    rows, begins = sweep(values, hours, profile, sizes, a.positions)
    print(f'series: {len(values)} samples; {len(begins)} locations; run = {CALM} calm + 1 spike + {AFTER} after '
          f'(first {de.MIN_SAMPLES} not judged)')
    print(f"{'size':>6} {'found':>9} {'accuracy':>9} {'TP':>4} {'FP':>5} {'FN':>4} {'TN':>6} {'Prec':>6} {'Rec':>6} {'F1':>6} {'FP calm':>8}")
    f = lambda v: '  -  ' if v is None else f'{v:.2f}'
    for r in rows:
        print(f"{r['size'] * 100:>5.0f}% {r['detected']:>4}/{r['runs']:<4} {r['accuracy']:>9.2f} {r['TP']:>4} {r['FP']:>5} "
              f"{r['FN']:>4} {r['TN']:>6} {f(r['precision']):>6} {f(r['recall']):>6} {f(r['f1']):>6} {r['fp_calm']:>8}")
    m = minimum_detectable([r for r in rows if r['size'] > 0], a.level)
    print(f"\nminimum detectable spike (accuracy >= {a.level}): "
          + (f'{m * 100:.0f}%' if m is not None else 'none of the sizes reached the level'))
    base = [r for r in rows if r['size'] == 0]
    if base:
        print(f"size 0 % = no injection: p1 fires at the spike position in {base[0]['detected']}/{base[0]['runs']} runs "
              '(the base rate of false alarms there; a size is only meaningful above it)')
    z = rows[0]
    judged_calm = len(begins) * (CALM - de.MIN_SAMPLES)
    print(f"\nfalse alarms in the calm part (no spike yet), {judged_calm} judged samples: "
          f"p1 {z['fp_calm']} ({z['fp_calm'] / judged_calm * 1000:.0f} per 1000), "
          f"p2 {z['p2_fp_calm']} ({z['p2_fp_calm'] / judged_calm * 1000:.0f} per 1000)")
    print('p2 = two consecutive samples over the limit, so ONE spike sample can never raise it (its recall is 0 by definition);'
          ' p2 matters for anomalies that last 2+ samples.')
    if a.per_location:
        locs = per_location(values, hours, profile, a.positions)
        print(f"\nfalse alarms of the calm part per location (no spike; {CALM - de.MIN_SAMPLES} judged samples each)")
        print(f"{'start':>6} {'local hour':>11} {'p1':>4} {'p2':>4}")
        for r in locs:
            print(f"{r['begin']:>6} {r['hour']:>11.2f} {r['p1']:>4} {r['p2']:>4}")
        print(f"{'total':>6} {'':>11} {sum(r['p1'] for r in locs):>4} {sum(r['p2'] for r in locs):>4}")
    if a.json:
        json.dump({'rows': rows, 'begins': [int(b) for b in begins]}, open(a.json, 'w'), indent=1)


if __name__ == '__main__':
    main()
