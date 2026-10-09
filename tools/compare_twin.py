#!/usr/bin/env python3
"""compare_twin.py -- does the digital twin show the same noise level (and daily shape) as the real data?

Inputs
  --csv     CSV exported from the twin's Grafana panel (Inspect -> Data -> Download CSV)
  --report  report.json written by dataset_to_seg.py for the real dataset
Method (the same measuring stick is applied to both series)
  1. For every point, take the median of its 2*w nearest neighbours (w = window from the report,
     the point itself left out), and compute  (value - median) / median.
  2. The standard deviation of these numbers is the "residual sigma".
  3. Compare the residual sigma of the twin with the residual sigma of the real data.
With --seg (a model that has a dailyProfile) the twin also follows a daily curve. Then, before step 1:
  a. the position of the CSV inside the simulated day is found (the phase that best matches the profile),
  b. the shape is reported (correlation with the profile, relative swing of twin and profile), and
  c. the series is divided by the profile curve, so the noise is measured on a flat series. (Without
     this the kinks of the compressed daily curve, 5 s per simulated hour, would inflate the residual.)
Only the AMPLITUDE of the noise is compared. Sampling intervals differ (real: 15 min, twin: 1 s),
so the time structure is not comparable.

Usage:  python tools/compare_twin.py --csv data/entsoe_twin_sim.csv --report report.json
"""
import argparse
import json
import math
import re
import sys

import numpy as np
import pandas as pd


def residuals(values, window, mode='relative'):
    """Leave-one-out rolling median (same as dataset_to_seg.py, method rolling_median).
    mode 'relative': (x - m) / m ; mode 'additive': x - m."""
    n = len(values)
    out = []
    for i in range(n):
        lo, hi = max(0, i - window), min(n, i + window + 1)
        neighbours = np.concatenate([values[lo:i], values[i + 1:hi]])
        if len(neighbours) < 4:
            continue
        base = np.median(neighbours)
        if mode == 'additive':
            out.append(values[i] - base)
        elif base != 0:
            out.append((values[i] - base) / base)
    return np.array(out)


STEPS_PER_HOUR = 5   # as in templates/edge_simulator.py.jinja: 1 simulated hour = 5 steps (1 s each)


def read_profile(seg_path):
    text = open(seg_path, encoding='utf-8').read()
    m = re.search(r'dailyProfile:\s*"([^"]+)"', text)
    if not m:
        sys.exit(f'{seg_path}: no dailyProfile found')
    prof = [float(x) for x in m.group(1).split(',')]
    if len(prof) != 24:
        sys.exit(f'{seg_path}: dailyProfile must have 24 values, got {len(prof)}')
    return np.array(prof)


def profile_curve(profile, steps):
    """Linear interpolation between hourly factors, as the simulator does."""
    pos = (np.asarray(steps, float) / STEPS_PER_HOUR) % 24
    i = pos.astype(int)
    frac = pos - i
    return profile[i] * (1 - frac) + profile[(i + 1) % 24] * frac


def fit_phase(v, profile):
    """Offset (in steps) of the first CSV row inside the simulated day that maximises the correlation."""
    idx = np.arange(len(v))
    best = (-2.0, 0)
    for phase in range(24 * STEPS_PER_HOUR):
        c = np.corrcoef(v, profile_curve(profile, idx + phase))[0, 1]
        if c > best[0]:
            best = (c, phase)
    return best[1], best[0]


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--csv', required=True)
    ap.add_argument('--report', required=True, help='report.json from dataset_to_seg.py')
    ap.add_argument('--variable', default='load_noise')
    ap.add_argument('--column', default=None, help='value column in the CSV (default: the second column)')
    ap.add_argument('--cloud-capacity', type=float, default=None,
                    help='SolarPark only: nameplate MW. The twin outputs P = cap*(1-cloud)*0.75, so the cloud '
                         'fraction is recovered as 1 - P/(0.75*cap) before measuring the residual '
                         '(use with the simulator run with WEATHER_SMOOTHING_ALPHA=1.0)')
    ap.add_argument('--seg', default=None,
                    help='model (.seg) with a dailyProfile: compare the daily shape and measure the noise '
                         'after dividing the profile out (needs at least one full simulated day = 120 rows)')
    args = ap.parse_args()

    # ---- real data side (from the report)
    entries = [e for e in json.load(open(args.report, encoding='utf-8')) if e['variable'] == args.variable]
    if not entries:
        sys.exit(f'variable {args.variable!r} not found in {args.report}')
    e = entries[0]
    if e.get('status') != 'ACCEPTED' or e.get('detrend') != 'rolling_median' or e.get('mode') not in ('relative', 'additive'):
        sys.exit(f"this script handles ACCEPTED rolling_median fits (relative or additive); got status={e.get('status')}, "
                 f"detrend={e.get('detrend')}, mode={e.get('mode')}")
    window = int(e['window'])
    ratio = float(e['white_noise_ratio'])
    sigma_fit = float(e['params']['sigma'])
    # sigma_fit is what ended up in the .seg; the residual sigma of the real data is what the detrender measured
    real_resid = sigma_fit * ratio if e.get('bias_corrected') else sigma_fit
    real_n = int(e['n_train'])

    # ---- twin side (from the CSV)
    df = pd.read_csv(args.csv)
    col = args.column or df.columns[1]
    v = pd.to_numeric(df[col], errors='coerce').dropna().to_numpy(float)
    if args.cloud_capacity:
        v = 1.0 - v / (0.75 * args.cloud_capacity)
    dyn_lines = []
    if args.seg:
        prof = read_profile(args.seg)
        if len(v) < 24 * STEPS_PER_HOUR:
            sys.exit(f'need at least {24 * STEPS_PER_HOUR} rows (one simulated day) to compare the daily shape, got {len(v)}')
        phase, corr = fit_phase(v, prof)
        curve = profile_curve(prof, np.arange(len(v)) + phase)
        swing_twin = (np.percentile(v, 95) - np.percentile(v, 5)) / np.mean(v)
        swing_prof = (np.percentile(curve, 95) - np.percentile(curve, 5)) / np.mean(curve)
        dyn_lines = [
            f'DAILY SHAPE (twin vs the dailyProfile in {args.seg})',
            f'  phase of first row in the simulated day : {phase} steps',
            f'  correlation twin vs profile             : {corr:.3f}',
            f'  swing (p95-p5)/mean  twin / profile     : {swing_twin:.3f} / {swing_prof:.3f}',
            '',
        ]
        v = v / curve * np.mean(v)       # flat series: the profile is divided out before measuring the noise
    r = residuals(v, window, e['mode'])
    twin_resid = float(r.std(ddof=1))

    # sampling error of a standard deviation: sigma / sqrt(2n)
    se_twin = twin_resid / math.sqrt(2 * len(r))
    se_real = real_resid / math.sqrt(2 * real_n)
    se = math.hypot(se_twin, se_real)
    z = (twin_resid - real_resid) / se
    diff_pct = 100.0 * (twin_resid / real_resid - 1.0)

    for ln in dyn_lines:
        print(ln)
    print(f'variable            : {args.variable}   (column "{col}", detrender: rolling median {e["mode"]}, window {window})')
    print(f'twin points         : {len(v)}')
    print()
    print(f'sigma written in .seg          : {sigma_fit:.5f}  ({"bias-corrected" if e.get("bias_corrected") else "NOT bias-corrected"})')
    print(f'detrender white-noise ratio    : {ratio:.3f}   (the detrender inflates white noise by this factor)')
    print()
    print(f'REAL data  residual sigma      : {real_resid:.5f}   (± {se_real:.5f})')
    print(f'TWIN       residual sigma      : {twin_resid:.5f}   (± {se_twin:.5f})')
    print(f'difference                     : {diff_pct:+.1f} %   (z = {z:+.1f}; |z| < 2 means within sampling error)')
    print()
    if abs(z) < 2:
        print('RESULT: the twin reproduces the noise level of the real data (within sampling error).')
    else:
        print('RESULT: the twin does NOT reproduce the noise level of the real data.')
        print(f'        the twin injects sigma = {sigma_fit:.5f}; with the same detrender it would need '
              f'sigma ~ {real_resid / ratio:.5f} to match (the real residual is not white noise, or the twin adds noise of its own).')


if __name__ == '__main__':
    main()
