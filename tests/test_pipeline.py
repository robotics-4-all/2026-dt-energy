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


if __name__ == '__main__':
    unittest.main()
