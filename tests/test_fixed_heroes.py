import itertools
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'app'))
import numpy as np
import pandas as pd
import kingshot_full_model_analysis as m


def experiment(optional_yang=False, fixed_two=False):
    pool = list('ABCDEFG') + (['Yang'] if optional_yang else [])
    squads = [('Yang',) + (('Thrud',) if fixed_two else ()) + s
              for s in itertools.combinations(pool, 2 if fixed_two else 3)]
    heroes = sorted(set(itertools.chain.from_iterable(squads)))
    cond = pd.DataFrame([{f'n_{h}': s.count(h) for h in heroes} for s in squads])
    rng = np.random.default_rng(517)
    cond['trials'] = 1000
    cond['wins'] = rng.binomial(1000, .45, len(cond))
    return cond, heroes


class FixedHeroTests(unittest.TestCase):
    def test_fixed_once_and_one_to_two(self):
        for optional in (False, True):
            cond, heroes = experiment(optional)
            adjusted, active, fixed = m.conditional_composition(cond, heroes)
            self.assertEqual(fixed, {'Yang': 1})
            self.assertEqual('Yang' in active, optional)
            self.assertTrue((adjusted[[f'n_{h}' for h in active]].sum(axis=1) == 3).all())
            base, ref, others, duplicates = m.build_base_design(adjusted, active)
            self.assertEqual(np.linalg.matrix_rank(base), base.shape[1])
            base_fit = m.fit_binomial_direct(adjusted, base)
            self.assertTrue(np.isfinite(base_fit.covariance).all())
            pairs_X, pairs = m.add_pair_terms(adjusted, active, base)
            fit, notice = m.fit_supported_layer(adjusted, pairs_X, base,
                lambda: m.fit_full_model(adjusted, active, pairs_X, pairs), 'Pair')
            self.assertIsNone(notice)
            self.assertIsNotNone(fit)
            self.assertTrue(np.isfinite(fit.covariance).all())
            triple_X, _ = m.add_triple_terms(adjusted, active, pairs_X)
            # Saturation is detected before fitting, without NaN covariance.
            fit, notice = m.fit_supported_layer(adjusted, triple_X, pairs_X,
                lambda: self.fail('Saturated model must not be fitted'), 'Triple')
            self.assertIsNone(fit)
            self.assertIn('zero residual degrees of freedom', notice)

    def test_pair_saturation_and_no_information(self):
        cond, heroes = experiment(fixed_two=True)
        adjusted, active, _ = m.conditional_composition(cond, heroes)
        base, *_ = m.build_base_design(adjusted, active)
        pair_X, pairs = m.add_pair_terms(adjusted, active, base)
        fit, notice = m.fit_supported_layer(adjusted, pair_X, base,
            lambda: self.fail('Saturated pair model must not be fitted'), 'Pair')
        self.assertIsNone(fit)
        self.assertIn('Pair synergy unavailable', notice)
        fit, notice = m.fit_supported_layer(adjusted, base, base,
            lambda: self.fail('Uninformative model must not be fitted'), 'Pair')
        self.assertIn('no independently estimable information', notice)

    def test_gui_warning_delivered_on_main_thread(self):
        import kingshot_gui as gui
        import json
        app = gui.KingshotApp(); app.withdraw()
        try:
            notice = 'Triple synergy unavailable: zero residual degrees of freedom.'
            app.output_queue.put('KINGSHOT_ANALYSIS_NOTICE:' + json.dumps(notice) + '\n')
            with patch.object(gui.messagebox, 'showwarning') as warning:
                app._drain_output()
                warning.assert_called_once_with('Synergy analysis unavailable', notice, parent=app)
        finally:
            app.destroy()


if __name__ == '__main__':
    unittest.main()
