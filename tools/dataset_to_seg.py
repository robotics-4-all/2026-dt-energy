#!/usr/bin/env python3
import argparse
import datetime as dt
import functools
import json
import math
import os
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET

import numpy as np
import pandas as pd
import yaml
from scipy import signal, stats

VARIABLE_SEMANTICS = {
    # v = v_nom * (1 + alpha*p_net + noise)                 (template line ~173)
    'voltage_noise':   dict(mode='relative', positive=False),
    # p = demand * factor * (1 + noise)                     (template line ~209)
    'load_noise':      dict(mode='relative', positive=False),
    # cloud = clip(U(0.1,0.4) + noise, 0, 1)  -> additive on a cloud FRACTION (~218)
    'solar_noise':     dict(mode='additive', positive=False),
    # soc += ... + noise                                    (template line ~247)
    'soc_noise':       dict(mode='additive', positive=False),
    # f_measured = f_grid + noise  (Hz)                     (template line ~489)
    'frequency_noise': dict(mode='additive', positive=False),
    # raw positive value sampled directly (m/s)             (template line ~226)
    'wind_speed':      dict(mode='raw', positive=True),
    # congestion = level + (noise - 0.5), noise in (0,1)    (template line ~285)
    'congestion_noise': dict(mode='raw', positive=True, unit_interval=True),
}

COMMON_MEASURED = dict(
    status='ACTIVE', measuredVoltage=None, measuredCurrent=0.0, powerFactor=0.95,
    frequency=50.0, activePower=0.0, reactivePower=0.0, lastUpdate=None,
    isControllable=False,
)
CLASS_SPECS = {
    'Industrial': dict(
        order=['id', 'name', 'voltageLevel', 'status', 'measuredVoltage', 'measuredCurrent',
               'powerFactor', 'frequency', 'activePower', 'reactivePower', 'lastUpdate',
               'isControllable', 'demandMW', 'currentConsumption', 'industryType', 'dailyProfile'],
        defaults=dict(COMMON_MEASURED, demandMW=0.0, currentConsumption=0.0, industryType='Generic')),
    'Residential': dict(
        order=['id', 'name', 'voltageLevel', 'status', 'measuredVoltage', 'measuredCurrent',
               'powerFactor', 'frequency', 'activePower', 'reactivePower', 'lastUpdate',
               'isControllable', 'demandMW', 'currentConsumption', 'numberOfResidents',
               'hasSmartAppliances', 'dailyProfile'],
        defaults=dict(COMMON_MEASURED, demandMW=0.0, currentConsumption=0.0,
                      numberOfResidents=1, hasSmartAppliances=False)),
    'SolarPark': dict(
        order=['id', 'name', 'voltageLevel', 'status', 'measuredVoltage', 'measuredCurrent',
               'powerFactor', 'frequency', 'activePower', 'reactivePower', 'lastUpdate',
               'isControllable', 'maxCapacityMW', 'currentOutputMW', 'panelType', 'cloudCover'],
        defaults=dict(COMMON_MEASURED, maxCapacityMW=1.0, currentOutputMW=0.0,
                      panelType='Monocrystalline', cloudCover=0.2)),
    'WindFarm': dict(
        order=['id', 'name', 'voltageLevel', 'status', 'measuredVoltage', 'measuredCurrent',
               'powerFactor', 'frequency', 'activePower', 'reactivePower', 'lastUpdate',
               'isControllable', 'maxCapacityMW', 'currentOutputMW', 'windSpeed', 'turbineCount'],
        defaults=dict(COMMON_MEASURED, maxCapacityMW=1.0, currentOutputMW=0.0,
                      windSpeed=7.0, turbineCount=1)),
    'BatteryStorage': dict(
        order=['id', 'name', 'voltageLevel', 'status', 'capacityMWh', 'stateOfCharge'],
        defaults=dict(status='ACTIVE', capacityMWh=1.0, stateOfCharge=0.5)),
}
ENUM_ATTRS = {'status'}  # printed without quotes
STRING_ATTRS = {'id', 'name', 'lastUpdate', 'industryType', 'panelType', 'dailyProfile'}


def _iso_minutes(res):
    """'PT15M' -> 15, 'PT60M' -> 60, 'PT1H' -> 60."""
    r = res.upper()
    if r.startswith('PT') and r.endswith('M'):
        return int(r[2:-1])
    if r.startswith('PT') and r.endswith('H'):
        return 60 * int(r[2:-1])
    raise ValueError(f'unsupported resolution {res!r}')


def _local(tag):
    return tag.rsplit('}', 1)[-1]


def load_entsoe_xml(path, series_filter=None):
    """Parse an IEC 62325 ENTSO-E document into (timestamp, value). Sparse
    point sequences are NOT forward-filled; gaps are reported instead."""
    root = ET.parse(path).getroot()
    rows, n_missing = [], 0
    for ts in root.iter():
        if _local(ts.tag) != 'TimeSeries':
            continue
        if series_filter:
            fields = {_local(c.tag): (c.text or '').strip() for c in ts}
            if any(fields.get(k) != str(v) for k, v in series_filter.items()):
                continue
        for period in ts:
            if _local(period.tag) != 'Period':
                continue
            start = res = None
            points = []
            for el in period:
                name = _local(el.tag)
                if name == 'timeInterval':
                    for s in el:
                        if _local(s.tag) == 'start':
                            start = pd.Timestamp(s.text)
                elif name == 'resolution':
                    res = _iso_minutes(el.text)
                elif name == 'Point':
                    d = {_local(c.tag): c.text for c in el}
                    points.append((int(d['position']), float(d['quantity'])))
            if start is None or res is None or not points:
                continue
            positions = [p for p, _ in points]
            n_missing += (max(positions) - min(positions) + 1) - len(positions)
            for pos, q in points:
                rows.append((start + pd.Timedelta(minutes=res * (pos - 1)), q))
    if not rows:
        raise ValueError(f'no points found in {path}')
    df = pd.DataFrame(rows, columns=['timestamp', 'value'])
    df = df.sort_values('timestamp').drop_duplicates('timestamp').reset_index(drop=True)
    df.attrs['gaps'] = int(n_missing)
    return df


def load_nasa_power_csv(path, missing=-999.0):
    with open(path, encoding='utf-8') as fh:
        lines = fh.read().splitlines()
    try:
        skip = next(i for i, ln in enumerate(lines) if ln.strip() == '-END HEADER-') + 1
    except StopIteration:
        skip = 0
    df = pd.read_csv(path, skiprows=skip)
    df = df.replace(missing, np.nan)
    if {'YEAR', 'MO', 'DY'}.issubset(df.columns):
        hr = df['HR'] if 'HR' in df.columns else 0
        df['timestamp'] = pd.to_datetime(
            dict(year=df.YEAR, month=df.MO, day=df.DY, hour=hr))
    return df


def load_source(src, base_dir):
    kind = src['loader']
    path = src.get('path')
    if path and not os.path.isabs(path):
        path = os.path.normpath(os.path.join(base_dir, path))
    if kind == 'duckdb':
        import duckdb
        con = duckdb.connect(path, read_only=True)
        df = con.execute(src['query']).fetchdf()
    elif kind == 'csv':
        df = pd.read_csv(path, **src.get('read_csv', {}))
    elif kind == 'parquet':
        paths = src.get('paths') or [src['path']]
        frames = []
        for p in paths:
            if not os.path.isabs(p):
                p = os.path.normpath(os.path.join(base_dir, p))
            frames.append(pd.read_parquet(p, columns=src.get('columns')))
        df = pd.concat(frames, ignore_index=True)
        tc = src.get('timestamp_column')
        if tc:
            df['timestamp'] = pd.to_datetime(df[tc], utc=True)
    elif kind == 'nasa_power_csv':
        df = load_nasa_power_csv(path)
    elif kind == 'entsoe_xml':
        df = load_entsoe_xml(path, src.get('series_filter'))
    else:
        raise ValueError(f'unknown loader {kind!r}')
    if df.empty:
        raise ValueError('dataset is empty after loading')
    return df

def apply_transform(df, spec):
    t = spec.get('transform')
    if not t:
        return df
    df = df.copy()
    if t['name'] == 'cloud_fraction_from_irradiance':
        # clear-sky proxy = maximum observed at the same hour of day (no external
        # clear-sky model needed); cloud fraction = 1 - irradiance/proxy.
        col = spec['column']
        hour = df['timestamp'].dt.hour
        ref = df.groupby(hour)[col].transform('max')
        keep = ref >= float(t.get('min_reference', 100.0))
        df = df[keep].copy()
        df[col] = (1.0 - df[col] / ref[keep]).clip(0.0, 1.0)
        return df
    raise ValueError(f"unknown transform {t['name']!r}")


def select_rows(df, spec):
    if spec.get('filter'):
        df = df.query(spec['filter'])
    df = apply_transform(df, spec)
    col = spec['column']
    df = df.dropna(subset=[col]).copy()
    tcol = spec.get('time_column')
    if spec.get('group_by') == '@date':          # one group per calendar day
        df['@date'] = df[tcol].dt.date
    if spec.get('group_by'):
        df = df.sort_values([spec['group_by']] + ([tcol] if tcol else []))
    elif tcol:
        df = df.sort_values(tcol)
    return df


def make_groups(df, spec):
    col = spec['column']
    if spec.get('group_by'):
        return [g[col].to_numpy(float) for _, g in df.groupby(spec['group_by'], sort=True)]
    return [df[col].to_numpy(float)]


def _resid(x, trend, mode):
    return (x - trend) / trend if mode == 'relative' else x - trend


def detrend_groups(groups, method, window, mode, polyorder=3):
    out = []
    for v in groups:
        n = len(v)
        if method == 'none':
            out.append(v.copy())
        elif method == 'savgol':
            w = int(window)
            if w % 2 == 0:
                w += 1
            if n < max(w + 4, 25):
                continue
            trend = signal.savgol_filter(v, w, polyorder)
            if mode == 'relative' and np.any(trend == 0):
                continue
            r = _resid(v, trend, mode)
            edge = w // 2
            out.append(r[edge:-edge])          # drop filter edge artefacts
        elif method == 'rolling_median':
            hw = int(window)
            r = np.full(n, np.nan)
            for i in range(n):
                lo, hi = max(0, i - hw), min(n, i + hw + 1)
                w = np.concatenate([v[lo:i], v[i + 1:hi]])
                if len(w) < 4:
                    continue
                b = np.median(w)
                if mode == 'relative' and b == 0:
                    continue
                r[i] = _resid(v[i], b, mode)
            out.append(r[~np.isnan(r)])
        else:
            raise ValueError(f'unknown detrend method {method!r}')
    return [g for g in out if len(g)]


@functools.lru_cache(maxsize=None)
def _rolling_median_ratio(half_window):
    """Exact-by-simulation std ratio of the leave-one-out rolling-median residual for white Gaussian
    noise (400,000 draws, fixed seed, precision ~0.1 %). The textbook sqrt(1 + pi/(2m)) is an
    asymptotic formula and overstates the inflation for the small m used here (m = 2w):
    w=2: 1.137 (formula 1.180), w=3: 1.103 (formula 1.123), w=8: 1.043 (formula 1.048)."""
    rng = np.random.default_rng(12345)
    x = rng.standard_normal((400_000, 2 * half_window + 1))
    neighbours = np.delete(x, half_window, axis=1)
    return float((x[:, half_window] - np.median(neighbours, axis=1)).std())


def white_noise_ratio(method, window, polyorder=3):
    """Std of the detrending residual divided by the std of the white noise that
    produced it, for pure white noise input. The residual of a detrender is not
    the noise itself:
      rolling_median (leave-one-out, m = 2w neighbours): ratio > 1, found by simulation (asymptotically sqrt(1 + pi/(2m)))
      savgol: residual = (I - S) x, variance (1 - c0) sigma^2, c0 = centre coeff < 1
    Reported so a fitted sigma is not mistaken for the sensor's true sigma."""
    if method == 'rolling_median':
        return _rolling_median_ratio(int(window))
    if method == 'savgol':
        w = int(window) + (int(window) % 2 == 0)
        c0 = float(signal.savgol_coeffs(w, polyorder)[w // 2])
        return math.sqrt(max(1e-9, 1.0 - c0))
    return 1.0


def chrono_split(groups, holdout):
    """Chronological split. Many groups (cycles, days): the first groups train, the
    last ones test. Few groups (one series, or a handful of feeders): every group
    is split in time, first part train, last part test."""
    if len(groups) >= 10:
        k = max(1, int(round(len(groups) * (1 - holdout))))
        return groups[:k], groups[k:]
    train, test = [], []
    for g in groups:
        k = int(round(len(g) * (1 - holdout)))
        train.append(g[:k])
        test.append(g[k:])
    return train, test


def thinned(groups):
    """Concatenate and thin to the effective sample size (lag-1 autocorrelation)."""
    flat = np.concatenate(groups)
    num = den = 0.0
    for g in groups:
        if len(g) < 3:
            continue
        d = g - g.mean()
        num += float(np.sum(d[:-1] * d[1:]))
        den += float(np.sum(d * d))
    rho = max(0.0, num / den) if den > 0 else 0.0
    rho = min(rho, 0.99)
    n_eff = max(30, int(len(flat) * (1 - rho) / (1 + rho)))
    stride = max(1, int(round(len(flat) / n_eff)))
    return flat[::stride], rho, len(flat)


# Families NOT in the grammar yet; only used with --explore-families (see main).
EXPERIMENTAL = []

CANDIDATES = {
    'NORMAL':    dict(k=2),
    'LOGNORMAL': dict(k=2),
    'GAMMA':     dict(k=2),
    'WEIBULL':   dict(k=2),
    'BETA':      dict(k=2),
}


def fit_family(name, x):
    """MLE fit in the DSL's own parametrisation (matches DIST_SAMPLERS in the
    simulator template). Returns (params dict, frozen scipy dist) or None."""
    try:
        if name == 'NORMAL':
            mu, sg = stats.norm.fit(x)
            return dict(mu=mu, sigma=sg), stats.norm(mu, sg)
        if name == 'LOGNORMAL':
            s, _, sc = stats.lognorm.fit(x, floc=0)
            return dict(mu=math.log(sc), sigma=s), stats.lognorm(s, 0, sc)
        if name == 'GAMMA':
            a, _, sc = stats.gamma.fit(x, floc=0)
            return dict(shape=a, scale=sc), stats.gamma(a, 0, sc)
        if name == 'WEIBULL':
            c, _, sc = stats.weibull_min.fit(x, floc=0)
            return dict(shape=c, scale=sc), stats.weibull_min(c, 0, sc)
        if name == 'BETA':
            a, b, _, _ = stats.beta.fit(x, floc=0, fscale=1)
            return dict(alpha=a, beta=b), stats.beta(a, b, 0, 1)
        if name == 'STUDENT_T':
            nu, mu, sg = stats.t.fit(x)
            return dict(nu=nu, mu=mu, sigma=sg), stats.t(nu, mu, sg)
        if name == 'LAPLACE':
            mu, b = stats.laplace.fit(x)
            return dict(mu=mu, scale=b), stats.laplace(mu, b)
    except Exception:
        return None
    return None


def candidate_families(x, sem):
    if not sem['positive'] and np.min(x) <= 0:
        return ['NORMAL'] + list(EXPERIMENTAL)
    fams = ['LOGNORMAL', 'GAMMA', 'WEIBULL']
    if sem.get('unit_interval') and np.min(x) > 0 and np.max(x) < 1:
        fams = ['BETA'] + fams
    return fams


def evaluate_window(train_groups, sem, alpha):
    """Fit every admissible family on TRAIN; return best passing + all rows."""
    train_flat = np.concatenate(train_groups)
    thin, rho, n_tr = thinned(train_groups)
    rows = []
    for fam in candidate_families(train_flat, sem):
        fit = fit_family(fam, train_flat)
        if fit is None:
            continue
        params, dist = fit
        ll = float(np.sum(dist.logpdf(train_flat)))
        if not np.isfinite(ll):
            continue
        ks = stats.kstest(thin, dist.cdf)
        rows.append(dict(family=fam, params=params, aic=2 * len(params) - 2 * ll,
                         ks_D=float(ks.statistic), ks_p=float(ks.pvalue),
                         n_train=n_tr, n_test=len(thin), rho1=rho,
                         passes=bool(ks.pvalue >= alpha)))
    passing = [r for r in rows if r['passes']]
    best_pass = min(passing, key=lambda r: r['aic']) if passing else None
    best_any = min(rows, key=lambda r: r['aic']) if rows else None
    return best_pass, best_any, rows


def calibrate_variable(var, spec, df, holdout, alpha, allow_rejected):
    sem = VARIABLE_SEMANTICS[var]
    mode = spec.get('residual', sem['mode'])
    method = spec.get('detrend', {}).get('method', 'none' if mode == 'raw' else 'rolling_median')
    if mode == 'raw':
        method = 'none'
    windows = spec.get('detrend', {}).get('windows', [0])
    polyorder = spec.get('detrend', {}).get('polyorder', 3)
    clip_k = spec.get('clip_mad')

    sub = select_rows(df, spec)
    base_groups = make_groups(sub, spec)
    result = dict(variable=var, mode=mode, detrend=method, n_input=int(sum(len(g) for g in base_groups)),
                  alpha=alpha, holdout=holdout, sweep=[], status='NO_DATA')
    if result['n_input'] < 60:
        result['reason'] = f"only {result['n_input']} usable samples (need >= 60)"
        return result

    chosen = None
    fallback = None
    for w in windows:
        res_groups = detrend_groups(base_groups, method, w, mode, polyorder)
        if not res_groups:
            continue
        train, test = chrono_split(res_groups, holdout)
        removed = 0
        if clip_k:
            tf = np.concatenate(train)
            med = float(np.median(tf))
            rs = 1.4826 * float(np.median(np.abs(tf - med)))
            lim = clip_k * rs

            def _clip(gs):
                return [g[np.abs(g - med) < lim] for g in gs if len(g)]
            n0 = sum(len(g) for g in train) + sum(len(g) for g in test)
            train, test = _clip(train), _clip(test)
            removed = n0 - sum(len(g) for g in train) - sum(len(g) for g in test)
        train = [g for g in train if len(g) > 3]
        test = [g for g in test if len(g) > 3]
        if not train or not test:
            continue
        best_pass, best_any, rows = evaluate_window(train, sem, alpha)
        entry = dict(window=w, clipped=int(removed),
                     white_noise_ratio=white_noise_ratio(method, w, polyorder),
                     n_train=int(sum(len(g) for g in train)),
                     n_holdout=int(sum(len(g) for g in test)),
                     candidates=rows)
        result['sweep'].append(entry)
        if best_pass and chosen is None:         # smallest window that passes TRAIN
            chosen = (w, best_pass, test, entry)
        if best_any and fallback is None:
            fallback = (w, best_any, test, entry)

    if chosen is None:
        result['status'] = 'REJECTED_TRAIN'
        result['reason'] = ('no admissible family passed the KS test on the train part '
                            f'(alpha={alpha}) for any window in {windows}')
        if allow_rejected and fallback:
            w, row, test, entry = fallback
            chosen, forced = (w, row, test, entry), True
        else:
            return result
    else:
        forced = False

    w, row, test, entry = chosen
    # frozen-parameter check on the hold-out
    fam, params = row['family'], row['params']
    dist = {
        'NORMAL': lambda p: stats.norm(p['mu'], p['sigma']),
        'LOGNORMAL': lambda p: stats.lognorm(p['sigma'], 0, math.exp(p['mu'])),
        'GAMMA': lambda p: stats.gamma(p['shape'], 0, p['scale']),
        'WEIBULL': lambda p: stats.weibull_min(p['shape'], 0, p['scale']),
        'BETA': lambda p: stats.beta(p['alpha'], p['beta'], 0, 1),
        'STUDENT_T': lambda p: stats.t(p['nu'], p['mu'], p['sigma']),
        'LAPLACE': lambda p: stats.laplace(p['mu'], p['scale']),
    }[fam](params)
    thin_t, rho_t, n_t = thinned(test)
    ks_t = stats.kstest(thin_t, dist.cdf)
    result.update(window=w, family=fam, params={k: float(v) for k, v in params.items()},
                  ks_train_p=row['ks_p'], ks_train_D=row['ks_D'],
                  ks_holdout_p=float(ks_t.pvalue), ks_holdout_D=float(ks_t.statistic),
                  n_train=entry['n_train'], n_holdout=entry['n_holdout'],
                  n_holdout_effective=len(thin_t), rho1_holdout=float(rho_t),
                  clipped=entry['clipped'], white_noise_ratio=entry['white_noise_ratio'],
                  bias_corrected=False)
    if fam == 'NORMAL' and mode != 'raw':
        # The detrender changes the residual std by `white_noise_ratio`; undo it so the sigma written to the .seg
        # is the white noise that, measured with the same detrender, gives the real residual (assumes white noise).
        result['params']['sigma'] = result['params']['sigma'] / entry['white_noise_ratio']
        result['bias_corrected'] = True
    if forced:
        result['status'] = 'FORCED_REJECTED'
    elif ks_t.pvalue >= alpha:
        result['status'] = 'ACCEPTED'
    else:
        result['status'] = 'REJECTED_HOLDOUT'
        result['reason'] = (f'passed on train (p={row["ks_p"]:.3f}) but failed on hold-out '
                            f'(p={ks_t.pvalue:.3f}): fit does not generalise')
        if allow_rejected:
            result['status'] = 'FORCED_REJECTED'
    return result


# --------------------------------------------------------------------------
# .seg writer
# --------------------------------------------------------------------------
def fnum(x):
    """Fixed-point (no exponent), 6 significant digits -> valid textX FLOAT."""
    x = float(f'{float(x):.6g}')
    s = np.format_float_positional(x, trim='0')
    if s.endswith('.'):
        s += '0'
    if '.' not in s:
        s += '.0'
    return s


def fmt_attr(key, val):
    if isinstance(val, bool):
        return 'true' if val else 'false'
    if key in ENUM_ATTRS:
        return str(val)
    if key in STRING_ATTRS or isinstance(val, str):
        return '"' + str(val).replace('"', "'") + '"'
    if isinstance(val, int) and key in ('turbineCount', 'numberOfResidents'):
        return str(val)
    return fnum(val)


def derive_attr(df, spec, base_dir):
    d = select_rows(df, spec)
    col = spec['column']
    stat = spec['stat']
    s = d[col]
    value = {'median': s.median, 'mean': s.mean, 'max': s.max, 'min': s.min,
             'last': lambda: s.iloc[-1]}[stat]() * float(spec.get('scale', 1.0))
    note = f"derived: {stat}({col}) over n={len(s)}"
    if spec.get('filter'):
        note += f", filter: {spec['filter']}"
    if spec.get('transform'):
        note += f", transform: {spec['transform']['name']}"
    return float(value), note


def derive_hourly_profile(df, spec):
    """24 hourly load factors from a time series: for each local hour h, the median of the values
    within +-30 min of h:00, divided by the mean of the 24 hourly medians (so the factors average 1.0).
    The factor is the typical level at the START of hour h relative to the daily average (the
    simulator interpolates linearly between hours).
    Timestamps without a zone are taken as UTC and converted to spec['timezone'] (default UTC)."""
    d = select_rows(df, spec)
    col, tcol = spec['column'], spec['time_column']
    ts = pd.to_datetime(d[tcol])
    ts = ts.dt.tz_localize('UTC') if ts.dt.tz is None else ts
    tz = spec.get('timezone', 'UTC')
    local = ts.dt.tz_convert(tz)
    minutes = (local.dt.hour * 60 + local.dt.minute).to_numpy()
    values = d[col].to_numpy(float)
    profile, counts = [], []
    for h in range(24):
        diff = ((minutes - h * 60 + 720) % 1440) - 720
        sel = values[(diff >= -30) & (diff <= 30)]
        if len(sel) < 5:
            raise ValueError(f'dailyProfile: only {len(sel)} points around hour {h} (need >= 5)')
        profile.append(float(np.median(sel)))
        counts.append(len(sel))
    norm = float(np.mean(profile))
    profile = [x / norm for x in profile]          # the 24 factors average exactly 1.0
    days = local.dt.date.nunique()
    note = (f"derived: hourly median of {col} (+-30 min around each local hour, {tz}), scaled so the 24 factors average 1.0; "
            f"{days} days, {min(counts)}-{max(counts)} points per hour; weekdays and weekends are mixed")
    text = ','.join(f'{x:.4f}' for x in profile)
    return text, note


def build_seg(mapping, df, base_dir, calibrations, source_desc):
    node = mapping['node']
    cls = node['class']
    spec = CLASS_SPECS[cls]
    attrs = dict(spec['defaults'])
    notes = {k: 'default (not derived from data)' for k in attrs}
    for key, val in (node.get('attrs') or {}).items():
        if isinstance(val, dict) and val.get('profile') == 'hour_of_day':
            attrs[key], notes[key] = derive_hourly_profile(df, val)
        elif isinstance(val, dict) and 'stat' in val:
            attrs[key], notes[key] = derive_attr(df, val, base_dir)
        else:
            attrs[key] = val
            notes[key] = 'literal from mapping'
    attrs['id'] = node['id']
    attrs['name'] = node.get('name', node['id'])
    notes['id'] = notes['name'] = 'literal from mapping'
    if attrs.get('measuredVoltage') is None and 'voltageLevel' in attrs:
        attrs['measuredVoltage'] = attrs['voltageLevel']
        notes['measuredVoltage'] = 'set equal to voltageLevel'
    if 'lastUpdate' in attrs and attrs['lastUpdate'] is None:
        attrs['lastUpdate'] = dt.date.today().isoformat()
        notes['lastUpdate'] = 'generation date'
    if 'voltageLevel' not in attrs:
        raise ValueError('mapping.node.attrs must define voltageLevel')

    ident = node.get('symbol', node['id'].upper().replace('-', '_'))
    grid = mapping.get('grid', {})
    hub = mapping.get('hub', {})
    hub_sym = hub.get('symbol', 'HUB_' + ident)
    out = []
    out.append('// ======================================================================')
    out.append('// Generated by dataset_to_seg.py -- do not edit the calibrated values by hand.')
    out.append(f'// Source: {source_desc}')
    out.append(f'// Generated: {dt.datetime.now().strftime("%Y-%m-%d %H:%M")}')
    out.append('// Elements other than the node below (hub substation, line) are literals,')
    out.append('// NOT derived from the dataset.')
    out.append('// ======================================================================')
    out.append(f"PowerGrid {grid.get('symbol', 'GRID_' + ident)}")
    out.append(f"gridName: \"{grid.get('name', node.get('name', ident))}\"")
    out.append(f"region: \"{grid.get('region', 'Unknown')}\"")
    out.append('end')
    out.append('')
    out.append(f'SubStation {hub_sym}')
    out.append(f"id: \"{hub.get('id', 'HUB-' + ident.replace('_', '-'))}\"")
    out.append(f"name: \"{hub.get('name', 'Hub substation')}\"")
    out.append(f"voltageLevel: {fnum(hub.get('voltageLevel', attrs['voltageLevel']))}")
    out.append('status: ACTIVE')
    out.append('congestionLevel: 0.1')
    out.append('lines {')
    out.append(f'    PowerLine LINE_{ident}')
    out.append(f'    id: "LINE-{ident.replace("_", "-")}"')
    out.append(f"    lengthKM: {fnum(hub.get('lineLengthKM', 1.0))}")
    out.append(f"    maxCapacityMW: {fnum(hub.get('lineCapacityMW', 1000.0))}")
    out.append(f'    source: {hub_sym}')
    out.append(f'    target: {ident}')
    out.append('    end')
    out.append('}')
    out.append('end')
    out.append('')
    out.append(f'{cls} {ident}')
    for key in spec['order']:
        if key not in attrs:      # optional attribute (dailyProfile) not requested by the mapping
            continue
        out.append(f'{key}: {fmt_attr(key, attrs[key])}   // {notes[key]}')
    dist_lines = []
    for c in calibrations:
        var = c['variable']
        if c['status'] in ('ACCEPTED', 'FORCED_REJECTED'):
            p = c['params']
            fam = c['family']
            if fam == 'NORMAL':
                text = f"NORMAL(mu={fnum(p['mu'])}, sigma={fnum(p['sigma'])})"
            elif fam == 'LOGNORMAL':
                text = f"LOGNORMAL(mu={fnum(p['mu'])}, sigma={fnum(p['sigma'])})"
            elif fam == 'GAMMA':
                text = f"GAMMA(shape={fnum(p['shape'])}, scale={fnum(p['scale'])})"
            elif fam == 'WEIBULL':
                text = f"WEIBULL(shape={fnum(p['shape'])}, scale={fnum(p['scale'])})"
            else:
                text = f"BETA(alpha={fnum(p['alpha'])}, beta={fnum(p['beta'])})"
            tag = 'WARNING KS-REJECTED, forced' if c['status'] == 'FORCED_REJECTED' else 'validated on hold-out'
            dist_lines.append(
                f"    // {var}: {tag} | window={c['window']} {c['detrend']} | "
                f"KS p train={c['ks_train_p']:.3f}, hold-out={c['ks_holdout_p']:.3f} | "
                f"n_train={c['n_train']}, n_holdout={c['n_holdout']} | "
                f"residual/white-noise std ratio={c['white_noise_ratio']:.2f}"
                f"{' | sigma = residual std / ratio (assumes the residual is white noise)' if c.get('bias_corrected') else ''}")
            dist_lines.append(f'    {var}: {text}')
        else:
            dist_lines.append(
                f"    // {var}: NOT CALIBRATED ({c['status']}): {c.get('reason', '')} -- system default applies")
    if dist_lines:
        out.append('distributions {')
        out.extend(dist_lines)
        out.append('}')
    out.append('end')
    out.append('')
    return '\n'.join(out)


# --------------------------------------------------------------------------
# Validation of the produced model
# --------------------------------------------------------------------------
def parse_check(seg_path, grammar_path):
    from textx import metamodel_from_file
    mm = metamodel_from_file(grammar_path)
    return mm.model_from_file(seg_path)


def run_generate_twin(generate_twin, templates, seg_path):
    with tempfile.TemporaryDirectory() as tmp:
        cmd = [sys.executable, os.path.abspath(generate_twin), os.path.abspath(seg_path),
               '--templates-dir', os.path.abspath(templates),
               '--grammar-path', os.path.abspath(os.path.join(os.path.dirname(generate_twin),
                                                              'grammar', 'smartenergygrid.tx')),
               '-v']
        p = subprocess.run(cmd, cwd=tmp, capture_output=True, text=True)
        return p.returncode, (p.stdout + p.stderr).strip()


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------
def print_report(calibrations):
    print('\nCalibration report')
    print('-' * 100)
    for c in calibrations:
        line = f"{c['variable']:16s} {c['status']:16s}"
        if 'family' in c:
            ps = ', '.join(f'{k}={v:.5g}' for k, v in c['params'].items())
            line += (f" {c['family']}({ps}) window={c['window']}"
                     f" KS p train={c['ks_train_p']:.3f} holdout={c['ks_holdout_p']:.3f}"
                     f" (D={c['ks_holdout_D']:.3f}) residual/white ratio={c['white_noise_ratio']:.2f}"
                     f"{' corrected' if c.get('bias_corrected') else ''}")
        if c.get('reason'):
            line += f"\n{'':34s}reason: {c['reason']}"
        print(line)
        for e in c['sweep']:
            for r in e['candidates']:
                ps = ', '.join(f'{k}={v:.5g}' for k, v in r['params'].items())
                print(f"{'':34s}  window={e['window']!s:>4} {r['family']:9s} {ps:34s}"
                      f" trainKS p={r['ks_p']:.3f} D={r['ks_D']:.3f} n={r['n_train']} n_eff={r['n_test']}"
                      f" rho1={r['rho1']:.2f}{'  PASS' if r['passes'] else ''}")
    print('-' * 100)


def run(args):
    with open(args.mapping, encoding='utf-8') as fh:
        mapping = yaml.safe_load(fh)
    base_dir = os.path.dirname(os.path.abspath(args.mapping))
    df = load_source(mapping['source'], base_dir)
    gaps = df.attrs.get('gaps')
    src = mapping['source']
    source_desc = f"{src['loader']} {os.path.basename(src.get('path', ''))}".strip()
    if mapping.get('description'):
        source_desc += f" -- {mapping['description']}"
    if gaps:
        source_desc += f" (note: {gaps} missing time steps inside the series)"

    calibrations = []
    for var, spec in (mapping.get('variables') or {}).items():
        if var not in VARIABLE_SEMANTICS:
            raise SystemExit(f'unknown noise variable {var!r}; valid: {sorted(VARIABLE_SEMANTICS)}')
        calibrations.append(calibrate_variable(var, spec, df, args.holdout, args.alpha,
                                               args.allow_rejected))

    print_report(calibrations)
    if getattr(args, 'explore_families', None):
        # exploration only: these families are not in the grammar, so no .seg is written
        if args.report:
            with open(args.report, 'w', encoding='utf-8') as fh:
                json.dump(calibrations, fh, indent=2, default=float)
        print('\n(exploration mode: no .seg written)')
        return
    seg_text = build_seg(mapping, df, base_dir, calibrations, source_desc)
    os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
    tmp_path = args.out + '.tmp'
    with open(tmp_path, 'w', encoding='utf-8', newline='\n') as fh:
        fh.write(seg_text)
    try:
        parse_check(tmp_path, args.grammar)
    except Exception as e:          # never leave an unparsable .seg behind
        os.remove(tmp_path)
        raise SystemExit(f'ERROR: generated model does not parse with the grammar:\n{e}')
    os.replace(tmp_path, args.out)
    print(f'\nWrote {args.out}  (parses OK with {os.path.basename(args.grammar)})')
    if args.report:
        with open(args.report, 'w', encoding='utf-8') as fh:
            json.dump(calibrations, fh, indent=2, default=float)
        print(f'Wrote {args.report}')
    if args.check_twin:
        code, log = run_generate_twin(args.check_twin, args.templates, args.out)
        print(f'generate_twin.py exit code: {code}')
        print('\n'.join(log.splitlines()[-12:]))
        if code != 0:
            raise SystemExit(1)


def self_test():
    """Recover known parameters from synthetic data (checks the fitting engine,
    the relative/additive handling and the hold-out logic)."""
    rng = np.random.default_rng(7)
    ok = True
    # (1) relative noise around a slow trend, NORMAL sigma=0.01
    n = 2000
    trend = 5000 + 800 * np.sin(np.linspace(0, 12, n))
    x = trend * (1 + rng.normal(0, 0.01, n))
    df = pd.DataFrame({'t': np.arange(n), 'v': x})
    r = calibrate_variable('load_noise', dict(column='v', time_column='t',
                           detrend=dict(method='rolling_median', windows=[2, 4])), df, 0.3, 0.05, False)
    s = r.get('params', {}).get('sigma')
    print('self-test 1 (relative NORMAL, true sigma 0.0100):', r['status'], s)
    ok &= r['status'] == 'ACCEPTED' and s is not None and abs(s - 0.01) < 0.003
    # (2) raw Weibull wind speed shape 2.0 scale 10.8
    w = 10.8 * rng.weibull(2.0, 4000)
    df = pd.DataFrame({'t': np.arange(len(w)), 'v': w})
    r = calibrate_variable('wind_speed', dict(column='v', time_column='t'), df, 0.3, 0.05, False)
    p = r.get('params', {})
    print('self-test 2 (raw WEIBULL, true shape 2.00 scale 10.80):', r['status'], r.get('family'), p)
    ok &= r['status'] == 'ACCEPTED' and r.get('family') in ('WEIBULL', 'GAMMA', 'LOGNORMAL')
    if r.get('family') == 'WEIBULL':
        ok &= abs(p['shape'] - 2.0) < 0.15 and abs(p['scale'] - 10.8) < 0.5
    # (3) a clearly non-normal signed noise must be REJECTED, not forced
    z = rng.standard_cauchy(2000) * 0.002
    df = pd.DataFrame({'t': np.arange(len(z)), 'v': 1000 * (1 + z)})
    r = calibrate_variable('load_noise', dict(column='v', time_column='t',
                           detrend=dict(method='rolling_median', windows=[2, 4])), df, 0.3, 0.05, False)
    print('self-test 3 (Cauchy noise must be rejected):', r['status'])
    ok &= r['status'].startswith('REJECTED')
    print('SELF-TEST', 'PASSED' if ok else 'FAILED')
    return 0 if ok else 1


def main(argv=None):
    ap = argparse.ArgumentParser(description='Convert a dataset into a validated .seg model.')
    ap.add_argument('--mapping', help='YAML mapping file (see tools/examples)')
    ap.add_argument('--grammar', default=os.path.join('grammar', 'smartenergygrid.tx'),
                    help='path to smartenergygrid.tx (default: grammar/smartenergygrid.tx)')
    ap.add_argument('--out', help='output .seg path')
    ap.add_argument('--holdout', type=float, default=0.3, help='chronological hold-out fraction (default 0.3)')
    ap.add_argument('--alpha', type=float, default=0.05, help='KS significance level (default 0.05)')
    ap.add_argument('--allow-rejected', action='store_true',
                    help='emit the best-AIC fit even if it fails validation (labelled WARNING)')
    ap.add_argument('--explore-families', default='',
                    help='comma list of families NOT in the grammar (STUDENT_T,LAPLACE) to include in the '
                         'candidate set; exploration only, no .seg is written')
    ap.add_argument('--report', help='write the full calibration report as JSON')
    ap.add_argument('--check-twin', metavar='generate_twin.py',
                    help='also run generate_twin.py on the result (in a temp dir)')
    ap.add_argument('--templates', default='templates', help='templates dir for --check-twin')
    ap.add_argument('--self-test', action='store_true', help='run synthetic recovery tests and exit')
    args = ap.parse_args(argv)
    if args.self_test:
        return self_test()
    if args.explore_families:
        EXPERIMENTAL[:] = [f.strip().upper() for f in args.explore_families.split(',') if f.strip()]
    elif not (args.mapping and args.out):
        ap.error('--mapping and --out are required')
    if not args.mapping:
        ap.error('--mapping is required')
    if not args.out:
        args.out = os.devnull
    run(args)
    return 0


if __name__ == '__main__':
    sys.exit(main())
