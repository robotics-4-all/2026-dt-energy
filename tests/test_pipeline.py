"""Regression tests for the .seg -> XMI -> twin pipeline.

Run from the project folder:   python -m unittest discover -s tests -v

What is covered
  1. Every example .seg parses, becomes an XMI that loads in the Ecore metamodel, passes all 18 constraints,
     and survives the round trip (.seg -> XMI -> reload) with 0 property differences.
  2. Models with deliberate errors are REJECTED, with the expected constraint id:
       C06 powerFactor outside (0, 1]    C02 duplicate element id    C16 non-positive distribution parameter
     and generate_twin.load_model refuses to produce a twin from them.
  3. A boolean written as `false` stays False (old bug: the string 'false' was truthy).
"""
import glob
import json
import os
import sys
import tempfile
import unittest

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
sys.path.insert(0, ROOT)
os.chdir(ROOT)

import seg2xmi            # noqa: E402
import generate_twin      # noqa: E402

GRAMMAR = os.path.join(ROOT, 'grammar', 'smartenergygrid.tx')
TEMPLATES = os.path.join(ROOT, 'templates')

EXAMPLES = sorted(
    [p for p in (os.path.join(ROOT, n) for n in
                 ('ptolemaida.seg', 'rural.seg', 'trikala.seg', 'ucsd.seg', 'visual_grid.seg')) if os.path.exists(p)]
    + glob.glob(os.path.join(ROOT, 'tools', 'examples', 'generated', '*.seg')))


def _write(tmp, name, text):
    path = os.path.join(tmp, name)
    with open(path, 'w', encoding='utf-8', newline='') as f:
        f.write(text)
    return path


def _read(name):
    with open(os.path.join(ROOT, name), encoding='utf-8', newline='') as f:
        return f.read()


class TestExamples(unittest.TestCase):
    def test_examples_found(self):
        self.assertGreaterEqual(len(EXAMPLES), 5, 'too few example .seg files found')

    def test_roundtrip_and_constraints(self):
        for path in EXAMPLES:
            with self.subTest(model=os.path.relpath(path, ROOT)):
                r = seg2xmi.roundtrip(path)
                self.assertEqual(r['violations'], [], 'constraint violations')
                self.assertEqual(r['only_in_seg'], [])
                self.assertEqual(r['only_in_xmi'], [])
                self.assertEqual(r['different'], [])
                self.assertGreater(r['properties'], 0)


class TestRejectedModels(unittest.TestCase):
    """The grammar (textX) accepts these; only the constraints catch them."""

    def _violations(self, text):
        with tempfile.TemporaryDirectory() as tmp:
            seg = _write(tmp, 'bad.seg', text)
            _, viol = seg2xmi.seg_to_xmi(seg, os.path.join(tmp, 'bad.xmi'), GRAMMAR, TEMPLATES)
            return seg, viol

    def _mutate(self, old, new):
        text = _read('ptolemaida.seg')
        self.assertIn(old, text, f'mutation anchor not found: {old!r}')
        return text.replace(old, new, 1)

    def test_power_factor_above_one_is_C06(self):
        _, viol = self._violations(self._mutate('powerFactor: 0.98', 'powerFactor: 1.5'))
        self.assertIn('C06', {v[0] for v in viol})

    def test_duplicate_element_id_is_C02(self):
        _, viol = self._violations(self._mutate('id: "WIND-KOZANI-1"', 'id: "PTL-V-STATION"'))
        self.assertIn('C02', {v[0] for v in viol})

    def test_negative_sigma_is_C16(self):
        _, viol = self._violations(self._mutate('NORMAL(mu=0.0, sigma=0.02)', 'NORMAL(mu=0.0, sigma=-0.02)'))
        self.assertIn('C16', {v[0] for v in viol})

    def test_generator_refuses_a_violating_model(self):
        text = self._mutate('powerFactor: 0.98', 'powerFactor: 1.5')
        with tempfile.TemporaryDirectory() as tmp:
            seg = _write(tmp, 'bad.seg', text)
            with self.assertRaises(RuntimeError) as ctx:
                generate_twin.load_model(seg, GRAMMAR, TEMPLATES, xmi_out=os.path.join(tmp, 'bad.xmi'))
            self.assertIn('C06', str(ctx.exception))

    def test_unchanged_model_has_no_violations(self):
        _, viol = self._violations(_read('ptolemaida.seg'))
        self.assertEqual(viol, [])


class TestBooleans(unittest.TestCase):
    def test_textx_gives_real_bools(self):
        """Root cause of the old bug: the grammar matches the TEXT 'false', and bool('false') is True."""
        text = _read('visual_grid.seg')
        with tempfile.TemporaryDirectory() as tmp:
            for value, expected in (('true', True), ('false', False)):
                seg = _write(tmp, f'{value}.seg', text.replace('hasSmartAppliances: true',
                                                               f'hasSmartAppliances: {value}'))
                model = seg2xmi.load_textx_metamodel(GRAMMAR).model_from_file(seg)
                found = [el.hasSmartAppliances for lst, _c in seg2xmi.ROOT_LISTS
                         for el in getattr(model, lst, []) if hasattr(el, 'hasSmartAppliances')]
                self.assertTrue(found)
                for v in found:
                    self.assertIsInstance(v, bool)
                    self.assertIs(v, expected)

    def test_false_stays_false_and_true_stays_true(self):
        text = _read('visual_grid.seg')
        self.assertIn('hasSmartAppliances: true', text)
        with tempfile.TemporaryDirectory() as tmp:
            for value in ('true', 'false'):
                seg = _write(tmp, f'{value}.seg', text.replace('hasSmartAppliances: true',
                                                               f'hasSmartAppliances: {value}'))
                xmi = os.path.join(tmp, f'{value}.xmi')
                seg2xmi.seg_to_xmi(seg, xmi, GRAMMAR, TEMPLATES)
                view = seg2xmi.xmi_to_view(seg2xmi.load_xmi(xmi))
                flags = []
                for lst, _cls in seg2xmi.ROOT_LISTS:
                    for el in getattr(view, lst, []):
                        if hasattr(el, 'hasSmartAppliances'):
                            flags.append(el.hasSmartAppliances)
                self.assertTrue(flags, 'no node with hasSmartAppliances found')
                self.assertIn(value == 'true', flags)
                if value == 'false':
                    self.assertNotIn(True, flags)


ENTSOE_SEG = os.path.join(ROOT, 'tools', 'examples', 'generated', 'entsoe_load_gr.seg')


class TestDailyProfile(unittest.TestCase):
    """dailyProfile: 24 hourly load factors in the .seg, used by the simulator (the daily curve of the twin)."""

    def _viol(self, text):
        with tempfile.TemporaryDirectory() as tmp:
            seg = _write(tmp, 'p.seg', text)
            _, viol = seg2xmi.seg_to_xmi(seg, os.path.join(tmp, 'p.xmi'), GRAMMAR, TEMPLATES)
            return {v[0] for v in viol}

    def _with_profile(self, new_value):
        import re
        text = open(ENTSOE_SEG, encoding='utf-8', newline='').read()
        self.assertRegex(text, r'dailyProfile: "[^"]+"')
        return re.sub(r'dailyProfile: "[^"]+"', f'dailyProfile: "{new_value}"', text, count=1)

    def test_generated_model_has_no_violations(self):
        self.assertEqual(self._viol(open(ENTSOE_SEG, encoding='utf-8', newline='').read()), set())

    def test_23_values_is_C18(self):
        self.assertIn('C18', self._viol(self._with_profile(','.join(['1.0'] * 23))))

    def test_non_positive_value_is_C18(self):
        self.assertIn('C18', self._viol(self._with_profile(','.join(['1.0'] * 23 + ['0.0']))))

    def test_not_a_number_is_C18(self):
        self.assertIn('C18', self._viol(self._with_profile(','.join(['1.0'] * 23 + ['abc']))))

    def test_profile_reaches_the_simulator_and_is_applied(self):
        """Generate the twin, import its simulator (MQTT client stubbed) and check the load follows the profile."""
        import types
        prof = None
        with tempfile.TemporaryDirectory() as tmp:
            old = os.getcwd()
            os.chdir(tmp)
            try:
                out = generate_twin.main([ENTSOE_SEG, '--templates-dir', TEMPLATES, '--grammar-path', GRAMMAR])
                out = out if isinstance(out, str) and os.path.isdir(out) else os.path.join(tmp, 'entsoe_load_gr_twin')
                stubs = {}
                for name in ('paho', 'paho.mqtt', 'paho.mqtt.client'):
                    stubs[name] = types.ModuleType(name)
                stubs['paho.mqtt.client'].Client = object
                stubs['paho.mqtt'].client = stubs['paho.mqtt.client']
                saved = {k: sys.modules.get(k) for k in stubs}
                sys.modules.update(stubs)
                sys.path.insert(0, out)
                try:
                    sim = __import__('edge_simulator')
                    grid = sim.build_topology()
                    node = grid.nodes['LOAD-GR-SYSTEM']
                    prof = node['daily_profile']
                    self.assertEqual(len(prof), 24)
                    # factor: exact at the start of an hour, midpoint half way, wraps after hour 23
                    self.assertAlmostEqual(sim.daily_profile_factor(prof, 0), prof[0])
                    self.assertAlmostEqual(sim.daily_profile_factor(prof, 5 * 7), prof[7])
                    self.assertAlmostEqual(sim.daily_profile_factor(prof, 5 * 7 + 2.5), (prof[7] + prof[8]) / 2)
                    self.assertAlmostEqual(sim.daily_profile_factor(prof, 5 * 23 + 2.5), (prof[23] + prof[0]) / 2)
                    self.assertAlmostEqual(sim.daily_profile_factor(prof, 5 * 24), prof[0])
                    # the twin load changes through the day: noise-free sigma is far smaller than the daily swing
                    values = []
                    for step in range(120):
                        sim.process_telemetry_physics(grid, step, {})
                        values.append(node['sim_active_power'])
                    swing = (max(values) - min(values)) / (sum(values) / len(values))
                    self.assertGreater(swing, 0.4)
                    # the published telemetry carries the simulated hour, so a detector can divide out the curve
                    sent = []

                    class _Client:
                        def publish(self, topic, message, qos=0):
                            sent.append(json.loads(message))
                    sim.publish_telemetry(_Client(), grid, 'T', 5 * 13 + 2)
                    load = [p for p in sent if p['node_id'] == 'LOAD-GR-SYSTEM'][0]
                    self.assertAlmostEqual(load['profile_hour'], 13.4)
                    hub = [p for p in sent if p['node_id'] == 'HUB-GR-SYSTEM-LOAD'][0]
                    self.assertNotIn('profile_hour', hub)           # nodes without a profile send no hour
                finally:
                    sys.path.remove(out)
                    sys.modules.pop('edge_simulator', None)
                    for k, v in saved.items():
                        if v is None:
                            sys.modules.pop(k, None)
                        else:
                            sys.modules[k] = v
            finally:
                os.chdir(old)


class TestNoiseRatio(unittest.TestCase):
    def test_rolling_median_ratio_is_the_simulated_one_not_the_asymptotic_formula(self):
        """w=2: simulated ratio 1.137 (the textbook sqrt(1+pi/(2m)) = 1.180 overstates it for small m)."""
        sys.path.insert(0, os.path.join(ROOT, 'tools'))
        import dataset_to_seg
        self.assertAlmostEqual(dataset_to_seg.white_noise_ratio('rolling_median', 2), 1.137, delta=0.004)
        self.assertAlmostEqual(dataset_to_seg.white_noise_ratio('rolling_median', 3), 1.103, delta=0.004)


class TestLoadDetectionReplay(unittest.TestCase):
    """The load detector of the generated analytics consumer, the replay tool and its evaluation."""

    @classmethod
    def setUpClass(cls):
        import ast
        from jinja2 import Environment, FileSystemLoader
        env = Environment(loader=FileSystemLoader(TEMPLATES))
        env.filters['pyrepr'] = repr
        text = env.get_template('analytics_consumer.py.jinja').render(load_profiles={})
        tree = ast.parse(text)
        keep = [n for n in tree.body
                if (isinstance(n, ast.Assign) and getattr(n.targets[0], 'id', '').startswith(('VOLTAGE_', 'LOAD_')))
                or (isinstance(n, ast.ClassDef) and n.name == 'ZScoreDetector')
                or (isinstance(n, ast.FunctionDef) and n.name == 'profile_factor')]
        ns = {}
        exec('import statistics\nfrom collections import deque', ns)
        exec(compile(ast.Module(body=keep, type_ignores=[]), 'analytics_consumer', 'exec'), ns)
        cls.TemplateDetector = ns['ZScoreDetector']
        cls.profile_factor = staticmethod(ns['profile_factor'])

    def _series(self):
        import numpy as np
        rng = np.random.default_rng(3)
        base = 5000 + 800 * np.sin(np.arange(400) / 25.0) + rng.normal(0, 25, 400)
        base[150] *= 1.3
        base[200:208] *= 1.15
        return base

    def test_template_detector_equals_offline_detector(self):
        sys.path.insert(0, os.path.join(ROOT, 'tools'))
        import detector_eval as de
        series = self._series()
        a, b = de.Detector(), self.TemplateDetector()
        for v in series:
            z, p1, p2 = a.step(float(v))
            res = b.step(float(v))
            if z is None:
                self.assertIsNone(res)
            else:
                self.assertAlmostEqual(res[0], z, places=9)
                self.assertEqual((res[3], res[4]), (p1, p2))

    def test_profile_factor_matches_the_simulator_and_the_offline_curve(self):
        sys.path.insert(0, os.path.join(ROOT, 'tools'))
        import numpy as np
        import detector_eval as de
        prof = [0.8 + 0.02 * h for h in range(24)]
        for hour in (0.0, 4.0, 13.4, 23.5, 24.0, 47.25):
            self.assertAlmostEqual(self.profile_factor(prof, hour), float(de.curve_at_hours(np.array(prof), [hour])[0]))

    def test_consumer_deseason_equals_offline_deseason(self):
        """Divide by the curve of the hour, then the template detector must equal the offline detector on the same series."""
        sys.path.insert(0, os.path.join(ROOT, 'tools'))
        import numpy as np
        import detector_eval as de
        prof = [0.8573, 0.8084, 0.7801, 0.7656, 0.7593, 0.7663, 0.8075, 0.8869, 0.9515, 1.0163, 1.0938, 1.2011,
                1.2584, 1.2837, 1.2516, 1.1689, 1.1036, 1.0598, 1.0371, 1.0553, 1.1190, 1.0742, 0.9818, 0.9125]
        rng = np.random.default_rng(5)
        hours = (np.arange(500) * 0.25) % 24
        load = 5300 * np.array([self.profile_factor(prof, h) for h in hours]) * (1 + rng.normal(0, 0.008, 500))
        load[300] *= 1.12
        a, b = de.Detector(), self.TemplateDetector()
        flagged = 0
        for v, h in zip(load, hours):
            x = v / self.profile_factor(prof, h)
            z, p1, p2 = a.step(float(x))
            res = b.step(float(x))
            if z is None:
                self.assertIsNone(res)
            else:
                self.assertAlmostEqual(res[0], z, places=9)
                flagged += int(p1)
        # the daily swing alone (0.76-1.28) does not trigger the detector once divided out
        self.assertLess(flagged, 15)

    def test_load_node_types_and_table_exist_in_generated_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            from jinja2 import Environment, FileSystemLoader
            env = Environment(loader=FileSystemLoader(TEMPLATES))
            env.filters['pyrepr'] = repr
            self.assertIn('load_zscore_eval', env.get_template('init_timescale.sql.jinja').render())
            consumer = env.get_template('analytics_consumer.py.jinja').render(load_profiles={'N': [1.0] * 24})
            self.assertIn("LOAD_DAILY_PROFILES = {'N':", consumer)
            self.assertIn('LOAD_NODE_TYPES = ("Residential", "Industrial")', consumer)
            self.assertIn('CREATE TABLE IF NOT EXISTS load_zscore_eval', consumer)

    def test_replay_events_and_evaluation_end_to_end(self):
        """Ground truth from the replay planner, rows produced by the detector, scored by evaluate_replay."""
        import numpy as np
        import pandas as pd
        sys.path.insert(0, os.path.join(ROOT, 'tools'))
        import replay_dataset as rp
        import evaluate_replay as ev
        rng = np.random.default_rng(1)
        prof = [0.8573, 0.8084, 0.7801, 0.7656, 0.7593, 0.7663, 0.8075, 0.8869, 0.9515, 1.0163, 1.0938, 1.2011,
                1.2584, 1.2837, 1.2516, 1.1689, 1.1036, 1.0598, 1.0371, 1.0553, 1.1190, 1.0742, 0.9818, 0.9125]
        hrs = (np.arange(629) * 0.25) % 24
        values = np.array([5300 * self.profile_factor(prof, h) for h in hrs]) * (1 + rng.normal(0, 0.008, 629))
        events = rp.place_events(len(values), rp.parse_plan(rp.DEFAULT_PLAN), seed=7)
        starts = sorted(e['start'] for e in events)
        self.assertTrue(all(b - a >= 50 for a, b in zip(starts, starts[1:])))
        stream, label = rp.build_stream(values, events)
        self.assertEqual(int((label > 0).sum()), sum(e['end'] - e['start'] for e in events))
        t0 = pd.Timestamp('2026-10-09T06:00:00Z')
        stamps = [(t0 + pd.Timedelta(seconds=i)).isoformat() for i in range(len(stream))]
        gt = {'node_id': 'LOAD-GR-SYSTEM', 'events': events, 'profile': prof,
              'samples': [{'index': i, 'timestamp': stamps[i], 'event_id': int(label[i]),
                           'real_value': float(values[i]), 'sent_value': float(stream[i]),
                           'profile_hour': float(hrs[i])} for i in range(len(stream))]}
        det = self.TemplateDetector()
        rows = []
        for i, v in enumerate(stream):
            r = det.step(float(v) / self.profile_factor(prof, hrs[i]))
            if r:
                rows.append({'timestamp': stamps[i], 'node_id': 'LOAD-GR-SYSTEM', 'z_score': r[0],
                             'predicted_p1': r[3], 'predicted_p2': r[4]})
        res = ev.evaluate(gt, pd.DataFrame(rows).assign(timestamp=lambda d: pd.to_datetime(d['timestamp'], utc=True)))
        big = [r for r in res['events'] if r['magnitude'] >= 0.20 and r['kind'] != 'drift']
        self.assertTrue(all(r['p1'] for r in big), big)          # a +20 % spike or shift is seen (a slow drift can be learned by the window and is not asserted)
        self.assertEqual(res['n_evaluated'], len(stream) - 10)   # the detector needs 10 samples of warm-up
        self.assertEqual(res['offline_mismatch'], 0)             # live rows == offline copy of the algorithm

    def test_single_experiment_and_per_sample_metrics(self):
        """One anomaly per run: 60 calm samples, the anomaly, then the rest; per-sample TP/FP/FN/TN add up."""
        import numpy as np
        import pandas as pd
        sys.path.insert(0, os.path.join(ROOT, 'tools'))
        import replay_dataset as rp
        import evaluate_replay as ev
        rng = np.random.default_rng(3)
        n = 400
        hrs = (np.arange(n) * 0.25) % 24
        values = 5300 * (1 + rng.normal(0, 0.008, n))
        b = rp.pick_segment(hrs, 60, 128, 12.0)
        self.assertAlmostEqual(hrs[b], 12.0, delta=0.125)
        begin, end, events = rp.single_experiment(values, hrs, 'shift', 0.30, 60, 60, 12.0)
        self.assertEqual(end - begin, 128)
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]['start'], 60)
        stream, label = rp.build_stream(values[begin:end], events)
        t0 = pd.Timestamp('2026-10-09T06:00:00Z')
        stamps = [(t0 + pd.Timedelta(seconds=i)).isoformat() for i in range(len(stream))]
        gt = {'node_id': 'X', 'events': events,
              'samples': [{'index': i, 'timestamp': stamps[i], 'real_value': float(values[begin + i]),
                           'sent_value': float(stream[i])} for i in range(len(stream))]}
        det = self.TemplateDetector()
        rows = []
        for i, v in enumerate(stream):
            r = det.step(float(v))
            if r:
                rows.append({'timestamp': stamps[i], 'node_id': 'X', 'z_score': r[0],
                             'predicted_p1': r[3], 'predicted_p2': r[4]})
        res = ev.evaluate(gt, pd.DataFrame(rows).assign(timestamp=lambda d: pd.to_datetime(d['timestamp'], utc=True)))
        m = res['persample']['p2']
        self.assertEqual(m['TP'] + m['FP'] + m['FN'] + m['TN'], len(stream) - 10)   # warm-up not judged
        self.assertEqual(m['TP'] + m['FN'], events[0]['end'] - events[0]['start'])
        self.assertEqual(m['fp_calm'], 0)
        self.assertGreater(m['TP'], 0)

    def test_spike_sweep_counts_and_base_rate(self):
        """sweep_spike: a huge spike is always found, size 0 equals the clean series, counts add up."""
        import numpy as np
        sys.path.insert(0, os.path.join(ROOT, 'tools'))
        import sweep_spike as sw
        rng = np.random.default_rng(5)
        n = 400
        values = 5300 * (1 + rng.normal(0, 0.008, n))
        hours = (np.arange(n) * 0.25) % 24
        prof = np.ones(24)
        rows, begins = sw.sweep(values, hours, prof, [0.0, 0.5], 10)
        zero, big = rows
        self.assertEqual(big['detected'], len(begins))
        self.assertEqual(big['FN'], 0)
        self.assertEqual(zero['TP'] + zero['FN'], len(begins))
        judged = len(begins) * (sw.CALM + 1 + sw.AFTER - 10)
        self.assertEqual(big['TP'] + big['FP'] + big['FN'] + big['TN'], judged)
        self.assertEqual(sw.minimum_detectable([r for r in rows if r['size'] > 0], 0.5), 0.5)

    def test_per_location_totals_equal_sweep_and_table_is_valid(self):
        """The per-location false alarms add up to the totals of the sweep; the drawio table is well-formed."""
        import numpy as np
        sys.path.insert(0, os.path.join(ROOT, 'tools'))
        import sweep_spike as sw
        import make_spike_table as mt
        rng = np.random.default_rng(11)
        n = 400
        values = 5300 * (1 + rng.normal(0, 0.02, n))
        hours = (np.arange(n) * 0.25) % 24
        prof = np.ones(24)
        rows, _ = sw.sweep(values, hours, prof, [0.0], 8)
        locs = sw.per_location(values, hours, prof, 8)
        self.assertEqual(sum(r['p1'] for r in locs), rows[0]['fp_calm'])
        self.assertEqual(sum(r['p2'] for r in locs), rows[0]['p2_fp_calm'])
        doc = mt.build(values, hours, prof, 0.5, 8)
        self.assertIn('Accuracy (= Recall)', doc)
        self.assertNotIn('Precision</', doc.replace('Precision·Recall', ''))   # no Precision column


if __name__ == '__main__':
    unittest.main()
