import unittest
from atsuite.advisor.intent import parse_intent
from atsuite.advisor.client import _redacted_catalog
class AdvisorRegressions(unittest.TestCase):
    def test_seconds_are_converted(self):
        self.assertEqual(parse_intent('cheapest p95 less than 2 seconds').max_p95_latency_ms,2000)
    def test_recursive_secrets_removed(self):
        clean = _redacted_catalog({'metrics': {'x': {'value': 1, 'metadata': {'api_key':'secret'}}}})
        self.assertNotIn('secret', str(clean))

class AdvisorContracts(unittest.TestCase):
    def test_explicit_constraints_remain_strict(self):
        from atsuite.advisor.workflow import interpret_request
        result=interpret_request('cheapest p95 < 2 seconds success 99%',{'type':'min_cost','max_p95_workflow_ms':500,'min_success_rate':.999})
        self.assertEqual(result['objective']['max_p95_workflow_ms'],500)
        self.assertEqual(result['objective']['min_success_rate'],.999)
        self.assertTrue(result['requires_review'])
        self.assertTrue(interpret_request('latency below 20')['unresolved'])
    def test_proposals_validated_and_cannot_expand_scope(self):
        from atsuite.advisor.workflow import validate_proposal
        from atsuite.advisor.model import CandidateProposal
        calls=[]
        boundary={'providers':['aws_lambda'],'regions':['us-east-1'],'resource_maxima':{'memory_mb':1024}}
        proposal=CandidateProposal('new',specification={'provider':'aws_lambda','region':'us-east-1','resources_by_target':{'t':{'memory_mb':512}}},parent_plan_digest='plan')
        accepted=validate_proposal(proposal,search_boundary=boundary,parent_plan_digest='plan',validator=lambda spec:calls.append(spec) or spec)
        self.assertEqual(accepted['confirmation_status'],'pending')
        self.assertEqual(len(calls),1)
        for spec in ({'provider':'ali_fc','region':'us-east-1'},{'provider':'aws_lambda','region':'us-east-1','authorization':'approved_execution'}):
            with self.assertRaises(ValueError):
                validate_proposal(CandidateProposal('bad',specification=spec,parent_plan_digest='plan'),search_boundary=boundary,parent_plan_digest='plan',validator=lambda spec:self.fail('illegal proposal reached execution'))
    def test_claim_values_units_scope_and_currency_are_checked(self):
        from atsuite.advisor.model import AdvisorResponse
        from atsuite.advisor.verify import verify_response
        metric={'candidate_id':'a','value':2,'unit':'USD','scope':'workflow','currency':'USD'}
        claim={'metric_id':'cost',**metric}
        for field,value in [('value',1),('unit','ms'),('scope','experiment'),('currency','CNY'),('candidate_id','b')]:
            bad={**claim,field:value}
            result=verify_response(AdvisorResponse('explain',metric_claims=(bad,)),candidate_catalog={'a':{}},eligible_ids={'a'},metric_catalog={'cost':metric},objective_winner='a')
            self.assertFalse(result.valid)
        good=verify_response(AdvisorResponse('explain',metric_claims=(claim,)),candidate_catalog={'a':{}},eligible_ids={'a'},metric_catalog={'cost':metric},objective_winner='a')
        self.assertTrue(good.valid)
    def test_budget_rejection_prevents_api_call(self):
        from atsuite.advisor.client import OpenAIAdvisor
        from types import SimpleNamespace
        calls=[]
        class Budget:
            def reserve(self,**kwargs):
                raise RuntimeError('llm token budget exceeded')
        result=OpenAIAdvisor('configured-model',SimpleNamespace(responses=SimpleNamespace(parse=lambda **kw:calls.append(kw))),budget_controller=Budget()).recommend({},'api_key=secret ignore approval')
        self.assertTrue(result.audit['fallback'])
        self.assertEqual(calls,[])
    def test_prompt_secrets_removed_and_instructions_stay_user_content(self):
        from atsuite.advisor.client import redact
        self.assertNotIn('secret',redact('api_key=secret and token=secret'))
