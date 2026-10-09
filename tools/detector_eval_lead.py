#!/usr/bin/env python3
"""detector_eval_lead.py -- the twin's Modified Z-score detector on LEAD1.0, a public dataset with
HUMAN-annotated anomalies in hourly electricity meter readings of 200 commercial buildings (2016).

Source of the data: https://github.com/samy101/lead-dataset (data/lead1.0-small.zip; paper arXiv:2203.17256).
The labels were drawn by hand by the dataset authors (point and sequential anomalies); they state that
"each building has a different definition of anomaly". So the labels are NOT a certain ground truth.

Method (same chronological rule as dataset_to_seg.py: learn on the first 70 %, test on the last 30 %)
  - the detector is the copy in detector_eval.py of templates/analytics_consumer.py.jinja
    (window 30, |z| > 3.5, p1 = one sample, p2 = two consecutive samples)
  - raw      : the meter readings as they are
  - daily24  : divided by the building's median reading per hour of day   (learned on the first 70 %)
  - weekly168: divided by the building's median reading per hour of week  (learned on the first 70 %)
  - an alarm at sample t counts as correct when a labelled anomaly lies within +-TOL samples
  - precision, recall and F1 are pooled over all buildings (micro) on the last 30 %
  Readings that are missing or <= 0 are skipped by the detector (the twin only tests voltage > 0).

Usage:  python tools/detector_eval_lead.py --csv /path/to/lead1.0-small.csv [--json out.json]
"""
import argparse
import json
import os
import sys
from concurrent.futures import ProcessPoolExecutor

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from detector_eval import Detector      # noqa: E402

TRAIN_FRACTION = 0.7
TOL = 2          # samples (hours) of tolerance between an alarm and a labelled anomaly
VARIANTS = ('raw', 'daily24', 'weekly168')


def seasonal_factors(values, period, n_train):
    """Median reading per position in the cycle, learned on the first n_train samples, scaled to mean 1."""
    idx = np.arange(len(values)) % period
    fac = np.full(period, np.nan)
    for k in range(period):
        v = values[:n_train][idx[:n_train] == k]
        v = v[~np.isnan(v) & (v > 0)]
        if len(v) >= 5:
            fac[k] = np.median(v)
    if np.isnan(fac).any() or not np.all(fac > 0):
        return None
    return fac / fac.mean()


def run_building(args):
    values, labels = args
    n = len(values)
    n_train = int(n * TRAIN_FRACTION)
    out = {}
    for variant in VARIANTS:
        if variant == 'raw':
            x = values.copy()
        else:
            fac = seasonal_factors(values, 24 if variant == 'daily24' else 168, n_train)
            if fac is None:
                out[variant] = None
                continue
            period = 24 if variant == 'daily24' else 168
            x = values / fac[np.arange(n) % period]
        det = Detector()
        p1 = np.zeros(n, bool)
        p2 = np.zeros(n, bool)
        for t in range(n):
            v = x[t]
            _, a, b = det.step(None if np.isnan(v) else float(v))
            p1[t], p2[t] = a, b
        res = {}
        near = np.convolve(labels[n_train:].astype(float), np.ones(2 * TOL + 1), 'same') > 0   # label within +-TOL
        for rule, alarm in (('p1', p1), ('p2', p2)):
            a = alarm[n_train:]
            lab = labels[n_train:].astype(bool)
            # hits: alarms with a label nearby; recall: labelled points with an alarm nearby
            alarm_near = np.convolve(a.astype(float), np.ones(2 * TOL + 1), 'same') > 0
            res[rule] = {'alarms': int(a.sum()), 'alarms_correct': int((a & near).sum()),
                         'labels': int(lab.sum()), 'labels_found': int((lab & alarm_near).sum()),
                         'test_samples': int(len(a))}
        out[variant] = res
    return out


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--csv', required=True)
    ap.add_argument('--json', default=None)
    args = ap.parse_args()
    df = pd.read_csv(args.csv, parse_dates=['timestamp']).sort_values(['building_id', 'timestamp'])
    jobs = []
    for _, g in df.groupby('building_id'):
        jobs.append((g['meter_reading'].to_numpy(float), g['anomaly'].to_numpy(int)))
    with ProcessPoolExecutor() as ex:
        results = list(ex.map(run_building, jobs, chunksize=4))
    print(f'buildings: {len(jobs)}; anomaly rate overall {df.anomaly.mean():.4f}; tolerance +-{TOL} h; '
          f'test = last {int((1 - TRAIN_FRACTION) * 100)} % of the year\n')
    summary = {}
    print(f"{'variant':10s} {'rule':4s} {'alarms':>8s} {'precision':>10s} {'recall':>8s} {'F1':>7s} "
          f"{'alarms per 1000 samples':>25s}  buildings used")
    for variant in VARIANTS:
        for rule in ('p1', 'p2'):
            rs = [r[variant][rule] for r in results if r[variant] is not None]
            al = sum(r['alarms'] for r in rs)
            ac = sum(r['alarms_correct'] for r in rs)
            lb = sum(r['labels'] for r in rs)
            lf = sum(r['labels_found'] for r in rs)
            ts = sum(r['test_samples'] for r in rs)
            prec = ac / al if al else float('nan')
            rec = lf / lb if lb else float('nan')
            f1 = 2 * prec * rec / (prec + rec) if (prec + rec) > 0 else float('nan')
            summary[f'{variant}/{rule}'] = {'alarms': al, 'precision': prec, 'recall': rec, 'f1': f1,
                                            'alarms_per_1000': al / ts * 1000, 'buildings': len(rs)}
            print(f"{variant:10s} {rule:4s} {al:8d} {prec:10.3f} {rec:8.3f} {f1:7.3f} {al / ts * 1000:25.1f}  {len(rs)}")
    base = sum(r['raw']['p1']['labels'] for r in results) / sum(r['raw']['p1']['test_samples'] for r in results)
    print(f'\nshare of labelled samples in the test part: {base:.4f} (an alarm at random would be correct about '
          f'{min(1, base * (2 * TOL + 1)):.3f} of the time)')
    if args.json:
        json.dump(summary, open(args.json, 'w'), indent=1)


if __name__ == '__main__':
    main()
