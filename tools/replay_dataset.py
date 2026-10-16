#!/usr/bin/env python3
"""replay_dataset.py -- end-to-end anomaly-detection test on a REAL dataset.

What it does
  1. Reads the real ENTSO-E load series (the dataset the twin was calibrated on).
  2. Injects labelled anomalies (spike / shift / drift, known position and size).
  3. Sends the series, one sample per --interval seconds, to the twin's Kafka topic
     'telemetry.raw', exactly like the edge simulator would (field 'active_power', MW), together with
     'profile_hour' = the local hour (0-24) of the real sample, so the detector can divide out the
     daily curve of the .seg (dailyProfile) before it checks the value.
  4. Writes the ground truth (which samples are anomalous) to a labels file.
The twin's analytics consumer then runs its Modified Z-score on 'active_power' of
the node (table load_zscore_eval, panels in Grafana). tools/evaluate_replay.py
compares those rows with the labels.

Before you run it (otherwise the simulator and the replay both feed the same node):
    docker compose stop edge-simulator
After the run:
    docker compose start edge-simulator

One experiment = ONE anomaly: a calm part of real data (--calm samples), the anomaly, then real data
again (--after samples). The calm part starts at --start-hour (local hour of the real data).
Restart the analytics consumer before each experiment, so that its window starts empty:
    docker compose restart analytics-consumer

Usage (one experiment):
    python tools/replay_dataset.py --xml data/entsoe_actual_total_load_GR_2026-09-25_to_10-01.xml --type shift --size 0.10 --dry-run
    python tools/replay_dataset.py --xml data/entsoe_actual_total_load_GR_2026-09-25_to_10-01.xml --type shift --size 0.10
    (pip install kafka-python once; Kafka is reachable on localhost:9092)
Old multi-anomaly mode over the whole series:  --plan spike:0.10,shift:0.20,...
"""
import argparse
import json
import os
import sys
import time
from datetime import datetime, timezone

import numpy as np
import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import detector_eval as de          # inject(), constants of the detector copy
import dataset_to_seg as d

DEFAULT_PLAN = ('spike:0.10,shift:0.10,drift:0.10,spike:0.20,'
                'shift:0.05,drift:0.20,spike:0.05,shift:0.20')
LENGTHS = {'spike': de.SPIKE_LEN, 'shift': de.SHIFT_LEN, 'drift': de.DRIFT_LEN}


def parse_plan(text):
    plan = []
    for item in text.split(','):
        kind, mag = item.strip().split(':')
        if kind not in LENGTHS:
            raise ValueError(f'unknown anomaly type {kind!r} (spike, shift, drift)')
        plan.append((kind, float(mag)))
    return plan


def place_events(n, plan, seed):
    """One start index per planned event, evenly spread, in a random order of the plan."""
    rng = np.random.default_rng(seed)
    lo = de.WINDOW + 10
    hi = n - max(LENGTHS.values()) - de.TAIL - 5
    spacing = (hi - lo) // len(plan)
    if spacing < 50:
        raise ValueError(f'series of {n} samples is too short for {len(plan)} events (need spacing >= 50, got {spacing})')
    order = rng.permutation(len(plan))
    events = []
    for i, k in enumerate(order):
        kind, mag = plan[k]
        start = lo + i * spacing + int(rng.integers(0, spacing // 3))
        events.append({'kind': kind, 'magnitude': mag, 'start': start})
    return events


def pick_segment(hours, calm, length, start_hour):
    """Index of the first real sample whose local hour is within 7.5 min of start_hour, such that the segment
    [index, index + length) fits in the series."""
    n = len(hours)
    diff = ((hours - start_hour + 12) % 24) - 12
    for i in np.where(np.abs(diff) <= 0.125)[0]:
        if i + length <= n:
            return int(i)
    raise ValueError(f'no segment of {length} samples starting near {start_hour:.2f} h fits in the {n}-sample series')


def single_experiment(values, hours, kind, size, calm, after, start_hour):
    """One anomaly after `calm` calm samples, followed by `after` samples. Returns (begin, end, events)
    with the segment values[begin:end]; event indexes are relative to the segment."""
    length = LENGTHS[kind] if kind else 0
    total = calm + length + after
    begin = pick_segment(hours, calm, total, start_hour)
    events = [{'kind': kind, 'magnitude': size, 'start': calm}] if kind else []
    return begin, begin + total, events


def build_stream(values, events):
    x = np.array(values, float)
    label = np.zeros(len(x), int)
    for k, ev in enumerate(events, 1):
        x, end = de.inject(x, ev['kind'], ev['start'], ev['magnitude'])
        ev['end'] = int(end)
        ev['id'] = k
        label[ev['start']:end] = k
    return x, label


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--xml', required=True, help='ENTSO-E Actual Total Load XML')
    ap.add_argument('--node-id', default='LOAD-GR-SYSTEM')
    ap.add_argument('--node-type', default='Industrial')
    ap.add_argument('--topic', default='telemetry.raw')
    ap.add_argument('--kafka', default='localhost:9092')
    ap.add_argument('--interval', type=float, default=1.0, help='seconds between samples (629 samples = ~10.5 min at 1.0)')
    ap.add_argument('--type', choices=sorted(LENGTHS), default=None, help='anomaly type of the experiment')
    ap.add_argument('--size', type=float, default=0.10, help='anomaly size as a fraction, 0.10 = +10 %%')
    ap.add_argument('--calm', type=int, default=60, help='real samples before the anomaly (the first 10 are the detector warm-up)')
    ap.add_argument('--after', type=int, default=60, help='real samples after the anomaly')
    ap.add_argument('--start-hour', type=float, default=12.0, help='local hour of the first sample (default 12 = midday, a calm part of the day)')
    ap.add_argument('--plan', default=None, help='OLD multi mode over the whole series: comma list kind:magnitude, e.g. spike:0.10,shift:0.20')
    ap.add_argument('--seg', default=os.path.join(HERE, 'examples', 'generated', 'entsoe_load_gr.seg'),
                    help='the .seg whose dailyProfile the twin uses (stored in the labels for the evaluation)')
    ap.add_argument('--timezone', default='Europe/Athens', help='time zone of the daily profile')
    ap.add_argument('--clean', action='store_true', help='no injection (calm + after only; measures false alarms)')
    ap.add_argument('--seed', type=int, default=7)
    ap.add_argument('--out-dir', default=os.path.join(HERE, '..', 'replay_results'))
    ap.add_argument('--dry-run', action='store_true', help='print the plan, send nothing')
    args = ap.parse_args()

    df = d.load_entsoe_xml(args.xml, {'businessType': 'A04'})
    values = df['value'].to_numpy(float)
    ts = pd.to_datetime(df['timestamp'])
    ts = ts.dt.tz_localize('UTC') if ts.dt.tz is None else ts
    local = ts.dt.tz_convert(args.timezone)
    hours = (local.dt.hour + local.dt.minute / 60).to_numpy(float)
    profile = de.read_profile(args.seg)
    if args.plan is not None:
        events = [] if args.clean else place_events(len(values), parse_plan(args.plan), args.seed)
        begin = 0
    else:
        if args.type is None and not args.clean:
            sys.exit('give --type (spike, shift, drift) for a one-anomaly experiment, or --clean, or --plan for the old multi mode')
        begin, end, events = single_experiment(values, hours, None if args.clean else args.type, args.size,
                                               args.calm, args.after, args.start_hour)
        values, hours = values[begin:end], hours[begin:end]
    stream, label = build_stream(values, events)

    when = f' (real data from {int(hours[0])}:{int(round((hours[0] % 1) * 60)):02d} local time)' if len(hours) else ''
    print(f'series: {len(values)} real samples{when}, node {args.node_id}, {len(events)} injected event(s)')
    for ev in events:
        print(f"  event {ev['id']:2d}: {ev['kind']:5s} +{ev['magnitude'] * 100:.0f}%  samples {ev['start']}..{ev['end'] - 1}")
    print(f'duration: ~{len(stream) * args.interval / 60:.1f} min at {args.interval} s/sample')
    if args.dry_run:
        print('[dry-run] nothing sent')
        return

    try:
        from kafka import KafkaProducer
    except ImportError:
        sys.exit('kafka-python missing: pip install kafka-python')
    producer = KafkaProducer(bootstrap_servers=args.kafka, value_serializer=lambda v: json.dumps(v).encode('utf-8'))

    started = datetime.now(timezone.utc)
    rows = []
    for i, v in enumerate(stream):
        ts = datetime.now(timezone.utc).isoformat()
        producer.send(args.topic, {'timestamp': ts, 'node_id': args.node_id, 'node_type': args.node_type,
                                   'active_power': float(v), 'profile_hour': round(float(hours[i]), 4)})
        rows.append({'index': i, 'timestamp': ts, 'real_value': float(values[i]), 'sent_value': float(v),
                     'profile_hour': float(hours[i]), 'event_id': int(label[i])})
        if i % 50 == 0:
            print(f'  sent {i}/{len(stream)}')
        time.sleep(args.interval)
    producer.flush(timeout=15)

    os.makedirs(args.out_dir, exist_ok=True)
    tag = started.strftime('%Y%m%dT%H%M%S')
    path = os.path.join(args.out_dir, f'replay_{tag}.json')
    json.dump({'node_id': args.node_id, 'topic': args.topic, 'started_utc': started.isoformat(),
               'events': events, 'samples': rows, 'profile': [float(x) for x in profile],
               'detector': {'window': de.WINDOW, 'min_samples': de.MIN_SAMPLES, 'critical': de.CRITICAL,
                            'persistence': de.PERSISTENCE}},
              open(path, 'w', encoding='utf-8'), indent=1)
    print(f'ground truth saved: {path}')
    print('next: python tools/evaluate_replay.py --labels', path)


if __name__ == '__main__':
    main()
