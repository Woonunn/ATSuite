"""Proposed safety regressions; de752f2 currently FAILS all four.
Run from a real ATSuite checkout with:
  PYTHONPATH=. uv run python /path/to/test_review_regressions.py
No cloud credentials or paid cloud calls are used.
"""
import unittest
from atsuite.recommender.contracts import EvidenceMetric, ObjectiveSpec
from atsuite.recommender.decision import decide


def metric(name, value, unit, count=300):
    return EvidenceMetric(name=name, value=value, unit=unit, scope='workflow',
                          sample_count=count, quality='available',
                          evidence_refs=('fixture:evidence',))


def candidate(cid, cost, currency='USD', count=300, include_latency=True, status='success'):
    metrics = {'cost': metric('cost', cost, currency, count),
               'failure_rate': metric('failure_rate', 0.0, 'ratio', count)}
    if include_latency:
        metrics['p95_workflow_ms'] = metric('p95_workflow_ms', 100., 'ms', count)
    return {'candidate_id': cid, 'scenario_id': 's1', 'metrics': metrics,
            'status': status, 'output_validation': 'failed' if status == 'failed' else 'passed',
            'state_validation': 'passed', 'phase': 'confirmation'}


class ReviewRegressions(unittest.TestCase):
    def test_zero_samples_must_not_be_verified(self):
        result = decide([candidate('zero', .1, count=0)], ObjectiveSpec(type='min_latency'))
        self.assertNotEqual(result.status, 'recommended')

    def test_failed_output_must_not_win(self):
        result = decide([candidate('bad', .1, status='failed'), candidate('good', .9)],
                        ObjectiveSpec(type='min_cost_under_slo'))
        self.assertNotEqual(result.best_verified, 'bad')

    def test_explicit_latency_constraint_requires_latency(self):
        result = decide([candidate('missing', .1, include_latency=False), candidate('good', .9)],
                        ObjectiveSpec(type='min_cost', max_p95_workflow_ms=1000))
        self.assertNotEqual(result.best_verified, 'missing')

    def test_mixed_currencies_require_normalization(self):
        result = decide([candidate('usd', .2), candidate('cny', 1., currency='CNY')],
                        ObjectiveSpec(type='min_cost', base_currency='USD'))
        self.assertNotEqual(result.status, 'recommended')


if __name__ == '__main__':
    unittest.main(verbosity=2)
