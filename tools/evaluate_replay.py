#!/usr/bin/env python3
"""evaluate_replay.py -- compare the ground truth of replay_dataset.py with what the twin's
detector wrote to load_zscore_eval.

  python tools/evaluate_replay.py --labels replay_results/replay_<time>.json
        (reads the twin's database on localhost:5432; pip install psycopg2-binary)
  python tools/evaluate_replay.py --labels ... --eval-csv load_eval.csv
        (a CSV exported from load_zscore_eval, columns timestamp,node_id,load_mw,median_v,mad_v,z_score,predicted_p2,predicted_p1)

Definitions
  event       : the injected samples start..end-1
  detected    : an alarm (p1 = one sample over the limit, p2 = two in a row) in [start, end+TAIL)
  false alarm : an alarm outside every event zone; zone = start .. end+WINDOW, because
                after an event the detector's window still holds the anomalous values
  delay       : samples from the start of the event to the first alarm
  the first MIN_SAMPLES samples are the detector's warm-up and are not counted
"""
import argparse
import json
import os
import sys

import numpy as np
import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import detector_eval as de


def load_eval(args, node_id, t0, t1):
    if args.eval_csv:
        df = pd.read_csv(args.eval_csv)
    else:
        try:
            import psycopg2
        except ImportError:
            sys.exit('psycopg2 missing: pip install psycopg2-binary (or use --eval-csv)')
        conn = psycopg2.connect(args.dsn)
        df = pd.read_sql('SELECT "timestamp", node_id, load_mw, z_score, predicted_p2, predicted_p1 '
                         'FROM load_zscore_eval WHERE node_id = %(n)s AND "timestamp" BETWEEN %(a)s AND %(b)s '
                         'ORDER BY 1', conn, params={'n': node_id, 'a': t0, 'b': t1})
        conn.close()
    df = df[df['node_id'] == node_id].copy()
    df['timestamp'] = pd.to_datetime(df['timestamp'], utc=True, format='ISO8601')
    for c in ('predicted_p1', 'predicted_p2'):
        df[c] = df[c].map(lambda v: v is True or str(v).lower() in ('t', 'true', '1'))
    return df


def sample_metrics(label, pred, skip, first, last):
    """Per-sample confusion matrix. label = injected sample; warm-up samples (skip) are not judged.
    FP is split: before the event (calm window) and after it."""
    ok = ~skip
    tp = int((pred & label & ok).sum())
    fp = int((pred & ~label & ok).sum())
    fn = int((~pred & label & ok).sum())
    tn = int((~pred & ~label & ok).sum())
    idx = np.arange(len(label))
    fp_calm = int((pred & ~label & ok & (idx < first)).sum())
    fp_after = int((pred & ~label & ok & (idx >= last)).sum())
    prec = tp / (tp + fp) if tp + fp else None
    rec = tp / (tp + fn) if tp + fn else None
    f1 = 2 * prec * rec / (prec + rec) if prec and rec else (0.0 if prec is not None and rec is not None else None)
    return {'TP': tp, 'FP': fp, 'FN': fn, 'TN': tn, 'precision': prec, 'recall': rec, 'f1': f1,
            'fp_calm': fp_calm, 'fp_after': fp_after}


def evaluate(gt, ev):
    samples = pd.DataFrame(gt['samples'])
    samples['timestamp'] = pd.to_datetime(samples['timestamp'], utc=True, format='ISO8601')
    m = samples.merge(ev[['timestamp', 'z_score', 'predicted_p1', 'predicted_p2']], on='timestamp', how='left')
    n = len(m)
    m['evaluated'] = m['z_score'].notna()
    live1 = m['predicted_p1'].fillna(False).to_numpy(bool)
    live2 = m['predicted_p2'].fillna(False).to_numpy(bool)
    have_values = 'sent_value' in m and 'real_value' in m
    if have_values:
        real = m['real_value'].to_numpy(float)
        sent = m['sent_value'].to_numpy(float)
        if gt.get('profile') and 'profile_hour' in m:
            # the detector divides by the daily curve first; so does the offline copy
            f = de.curve_at_hours(np.array(gt['profile']), m['profile_hour'].to_numpy(float))
            real, sent = real / f, sent / f
        # what the detector would say on the CLEAN real series, and on the series as sent (same algorithm, offline copy)
        clean1, clean2 = de.run_detector(real)
        off1, off2 = de.run_detector(sent)
        ok = m['evaluated'].to_numpy(bool)
        mismatch = int(((off1 != live1) | (off2 != live2))[ok].sum())
        # only alarms CAUSED by the injection count as detections
        p1, p2 = live1 & ~clean1, live2 & ~clean2
    else:
        clean1 = clean2 = np.zeros(n, bool)
        mismatch = None
        p1, p2 = live1, live2
    zone = np.zeros(n, bool)
    rows = []
    for e in gt['events']:
        s, end = e['start'], e['end']
        zone[s:min(n, end + de.WINDOW)] = True
        win = slice(s, end + de.TAIL)
        r = {'event': e['id'], 'kind': e['kind'], 'magnitude': e['magnitude'], 'start': s, 'len': end - s}
        for name, arr in (('p1', p1), ('p2', p2)):
            hit = arr[win]
            r[name] = bool(hit.any())
            r[name + '_delay'] = int(np.argmax(hit)) if hit.any() else None
        rows.append(r)
    outside = ~zone
    outside[:de.MIN_SAMPLES] = False
    skip = np.arange(n) < de.MIN_SAMPLES
    label = np.zeros(n, bool)
    for e in gt['events']:
        label[e['start']:e['end']] = True
    first = min([e['start'] for e in gt['events']], default=n)
    last = max([e['end'] for e in gt['events']], default=0)
    persample = {}
    for name, arr in (('p1', live1), ('p2', live2)):
        persample[name] = sample_metrics(label, arr, skip, first, last)
    out = {'persample': persample, 'n_sent': n, 'n_evaluated': int(m['evaluated'].sum()), 'events': rows,
           'false_p1': int((live1 & outside).sum()), 'false_p2': int((live2 & outside).sum()),
           'n_outside': int(outside.sum()),
           'clean_series_p1': int((clean1 & ~skip).sum()), 'clean_series_p2': int((clean2 & ~skip).sum()),
           'offline_mismatch': mismatch}
    return out


def report(res):
    expected = max(0, res['n_sent'] - de.MIN_SAMPLES)    # the detector writes no row during its warm-up
    print(f"samples sent: {res['n_sent']}, found in the database: {res['n_evaluated']} "
          f"(expected {expected} = sent minus the {de.MIN_SAMPLES} warm-up samples)")
    if res['n_evaluated'] < expected * 0.95:
        print('WARNING: more than 5 % of the sent samples have no row in load_zscore_eval; the result is incomplete '
              '(is the analytics-consumer running? is the node id right?)')
    print(f"\n{'event':>5} {'type':>6} {'size':>6} {'len':>4}   {'p1':>14} {'p2 (alert)':>14}")
    for r in res['events']:
        def cell(name):
            return ('DETECTED' + (f" (+{r[name + '_delay']})" if r[name + '_delay'] is not None else '')) if r[name] else 'missed'
        print(f"{r['event']:>5} {r['kind']:>6} {r['magnitude'] * 100:>5.0f}% {r['len']:>4}   {cell('p1'):>14} {cell('p2'):>14}")
    ev = res['events']
    if ev:
        print(f"\ndetected: p1 {sum(r['p1'] for r in ev)}/{len(ev)}   p2 {sum(r['p2'] for r in ev)}/{len(ev)}")
    k = res['n_outside']
    print(f"false alarms outside the event zones ({k} samples): p1 {res['false_p1']}, p2 {res['false_p2']}"
          + (f"  ({res['false_p2'] / k * 1000:.1f} p2 per 1000 samples)" if k else ''))
    if res['offline_mismatch'] is not None:
        print(f"detector on the CLEAN real series (no injection), same algorithm offline: p1 {res['clean_series_p1']}, "
              f"p2 {res['clean_series_p2']} alarms in {res['n_sent']} samples "
              f"({res['clean_series_p2'] / res['n_sent'] * 1000:.1f} p2 per 1000)")
        print(f"live rows that differ from the offline copy of the detector on the same samples: {res['offline_mismatch']}"
              ' (0 = the generated consumer behaves exactly like the evaluated algorithm)')
    ps = res.get('persample')
    if ps:
        f = lambda v: '  -  ' if v is None else f'{v:.3f}'
        print('\nPer-sample metrics (an injected sample is positive; the first %d warm-up samples are not judged)' % de.MIN_SAMPLES)
        print(f"{'rule':>10} {'TP':>4} {'FP':>4} {'FN':>4} {'TN':>4} {'Precision':>10} {'Recall':>8} {'F1':>7}  FP calm / after")
        for name, label in (('p1', 'p1 (1 sample)'), ('p2', 'p2 (alert)')):
            m = ps[name]
            print(f"{label:>10} {m['TP']:>4} {m['FP']:>4} {m['FN']:>4} {m['TN']:>4} {f(m['precision']):>10} "
                  f"{f(m['recall']):>8} {f(m['f1']):>7}  {m['fp_calm']} / {m['fp_after']}")
        print('Note: alarms after the event can be late echoes (the detector window still holds anomalous values); counted as FP.')
    print('\ndetected = an alarm the clean series does not already have, inside the event window.')


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--labels', required=True)
    ap.add_argument('--eval-csv', default=None)
    ap.add_argument('--dsn', default='host=localhost port=5432 dbname=smartgrid_dt user=dt_user password=dt_password')
    ap.add_argument('--json', default=None)
    args = ap.parse_args()
    gt = json.load(open(args.labels, encoding='utf-8'))
    ts = pd.to_datetime([s['timestamp'] for s in gt['samples']], utc=True, format='ISO8601')
    ev = load_eval(args, gt['node_id'], ts.min() - pd.Timedelta(seconds=5), ts.max() + pd.Timedelta(seconds=5))
    res = evaluate(gt, ev)
    report(res)
    if args.json:
        json.dump(res, open(args.json, 'w', encoding='utf-8'), indent=1)


if __name__ == '__main__':
    main()
