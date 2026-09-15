"""Run with python -m unittest discover -s tests, from the suite folder."""
import itertools
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'app'))
import numpy as np
import pandas as pd
from scipy.linalg import null_space
import kingshot_full_model_analysis as m


class IdentificationTests(unittest.TestCase):
    def check_equivalent_fit(self, cond, X, fit):
        A = X.to_numpy(float)
        U, s, _ = np.linalg.svd(A, full_matrices=False)
        rank = np.linalg.matrix_rank(A)
        direct_X = pd.DataFrame(U[:, :rank])
        direct = m.fit_binomial_direct(cond, direct_X)
        self.assertTrue(np.isfinite(fit.covariance).all())
        np.testing.assert_allclose(fit.prediction, direct.prediction, atol=1e-8)
        np.testing.assert_allclose(
            np.einsum('ij,jk,ik->i', A, fit.covariance, A),
            np.einsum('ij,jk,ik->i', direct_X, direct.covariance, direct_X),
            atol=1e-8,
        )
        self.assertEqual(fit.df_resid, len(X) - rank)

    def test_mixed_design_pair_and_triple(self):
        heroes = list('ABCDEFG')
        squads = [s for s in itertools.combinations_with_replacement(heroes, 4)
                  if all(s.count(h) <= (3 if h == 'G' else 1) for h in heroes)]
        cond = pd.DataFrame([{f'n_{h}': s.count(h) for h in heroes} for s in squads])
        cond = pd.concat([cond, cond], ignore_index=True)
        rng = np.random.default_rng(17)
        cond['trials'] = 1000
        cond['wins'] = rng.binomial(1000, .4, len(cond))
        base, *_ = m.build_base_design(cond, heroes)
        pair, pairs = m.add_pair_terms(cond, heroes, base)
        triple, triples = m.add_triple_terms(cond, heroes, pair)
        self.assertLess(np.linalg.matrix_rank(pair), pair.shape[1])
        for X, fit in [(pair, m.fit_full_model(cond, heroes, pair, pairs)),
                       (triple, m.fit_full_triple_model(cond, heroes, triple, pairs, triples))]:
            self.check_equivalent_fit(cond, X, fit)
            okay, R = m.minimal_interaction_constraints(X)
            self.assertTrue(okay)
            B = null_space(R)
            self.assertEqual(B.shape[1], np.linalg.matrix_rank(X))
            # Changing column order must not choose different predictions/effects.
            shuffled = X.sample(frac=1, axis=1, random_state=12)
            okay, Rs = m.minimal_interaction_constraints(shuffled)
            self.assertTrue(okay)
            a = m.fit_minimal_interactions(cond, X, R)
            b = m.fit_minimal_interactions(cond, shuffled, Rs)
            np.testing.assert_allclose(a.params, b.params.reindex(X.columns), atol=1e-8)

    def test_rejects_lower_order_alias(self):
        X = pd.DataFrame({'const': [1., 1., 1., 1.], 'n_A': [1., 1., 1., 1.],
                          'pair_A_B': [0., 1., 0., 1.]})
        self.assertFalse(m.minimal_interaction_constraints(X)[0])

    def test_full_rank_needs_no_constraints(self):
        self.assertFalse(m.minimal_interaction_constraints(pd.DataFrame(np.eye(3)))[0])


if __name__ == '__main__':
    unittest.main()
