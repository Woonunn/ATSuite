"""DeepSeek official API contracts; HTTP transport is explicitly simulated."""
import contextlib
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import httpx
from openai import OpenAI as RealOpenAI
from atsuite.advisor import client as advisors
from atsuite.recommender.contracts import ExperimentSpec
from tools import advisor_check


CATALOG={'candidates':{},'eligible_candidates':[],'objective_winner':None,'metrics':{},'decision_status':'insufficient_evidence'}
OUTPUT={'objective_interpretation':'No verified measurements are supplied.', 'candidate_proposals':[],
    'preferred_candidate_id':None,'reason_codes':['missing_evidence'],'metric_references':[],
    'tradeoffs':[],'additional_experiments':[],'metric_claims':[]}


def response_body(output=OUTPUT, status='completed'):
    return {'id':'resp_contract','object':'response','created_at':1,'status':status,'model':'deepseek-flash',
        'output':[{'type':'message','id':'msg_contract','status':'completed','role':'assistant',
            'content':[{'type':'output_text','text':json.dumps(output),'annotations':[]}]}],
        'usage':{'input_tokens':10,'output_tokens':20,'total_tokens':30}}


class DeepSeekAdvisorContracts(unittest.TestCase):
    def test_explicit_empty_model_does_not_inherit_configured_global_model(self):
        calls=[]
        with patch.dict(os.environ,{'ATSUITE_ADVISOR_MODEL':'configured'}):
            result=advisors.OpenAIAdvisor(model='',client=SimpleNamespace(responses=SimpleNamespace(parse=lambda **kw:calls.append(kw)))).recommend(CATALOG)
        self.assertEqual(calls,[])
        self.assertTrue(result.audit['fallback'])

    def test_official_host_key_model_schema_and_usage_through_real_sdk(self):
        requests=[];options=[]
        def transport(request):
            requests.append(request)
            return httpx.Response(200,json=response_body())
        def constructor(**kwargs):
            options.append(kwargs)
            return RealOpenAI(**kwargs,http_client=httpx.Client(transport=httpx.MockTransport(transport)))
        with patch.dict(os.environ,{'DEEPSEEK_API_KEY':'contract-deepseek-key','OPENAI_API_KEY':'other-provider-key',
            'OPENAI_BASE_URL':'https://wrong.example','ATSUITE_ADVISOR_MODEL':''}),patch('openai.OpenAI',side_effect=constructor):
            result=advisors.create_advisor(provider='deepseek').recommend(CATALOG,'api_key=private-prompt')
        self.assertEqual(result.provider,'deepseek');self.assertEqual(result.model,'deepseek-flash')
        self.assertTrue(result.valid);self.assertFalse(result.audit['fallback'])
        self.assertEqual(result.audit['usage']['total_tokens'],30)
        self.assertEqual(options[0]['api_key'],'contract-deepseek-key')
        self.assertEqual(options[0]['base_url'],'https://api.deepseek.com')
        self.assertEqual(options[0]['max_retries'],0)
        self.assertEqual(len(requests),1)
        request=requests[0];body=json.loads(request.content)
        self.assertEqual(str(request.url),'https://api.deepseek.com/responses')
        self.assertEqual(body['model'],'deepseek-flash')
        self.assertEqual(body['text']['format']['type'],'json_schema')
        self.assertTrue(body['text']['format']['strict'])
        self.assertEqual(body['reasoning']['effort'],'none')
        self.assertNotIn('private-prompt',str(body))
        self.assertNotIn('contract-deepseek-key',str(result.to_dict()))

    def test_missing_deepseek_key_never_uses_openai_key_or_reserves_budget(self):
        budget=SimpleNamespace(reserve=lambda **kw:self.fail('missing key spent budget'))
        with patch.dict(os.environ,{'OPENAI_API_KEY':'other-provider-key'},clear=True),patch('openai.OpenAI') as constructor:
            result=advisors.create_advisor(provider='deepseek',budget_controller=budget).recommend(CATALOG)
        constructor.assert_not_called();self.assertTrue(result.audit['fallback'])
        self.assertEqual(result.audit['error'],'missing_deepseek_api_key')

    def test_disabled_advisor_never_calls_api_with_key_and_model_set(self):
        with patch.dict(os.environ,{'DEEPSEEK_API_KEY':'contract-key','ATSUITE_ADVISOR_MODEL':'deepseek-flash'}),patch('openai.OpenAI') as constructor:
            result=advisors.create_advisor(provider='deepseek',enabled=False).recommend(CATALOG)
        constructor.assert_not_called();self.assertEqual(result.provider,'template')

    def test_rejection_timeout_and_truncation_do_not_count_as_live_success(self):
        for outcome in ('bad_claim','timeout','incomplete'):
            with self.subTest(outcome=outcome):
                def transport(request):
                    if outcome=='timeout':raise httpx.ReadTimeout('contract timeout',request=request)
                    output={**OUTPUT,'metric_references':['invented']} if outcome=='bad_claim' else OUTPUT
                    return httpx.Response(200,json=response_body(output,'incomplete' if outcome=='incomplete' else 'completed'))
                client=RealOpenAI(api_key='contract-key',base_url='https://api.deepseek.com',max_retries=0,
                    http_client=httpx.Client(transport=httpx.MockTransport(transport)))
                result=advisors.create_advisor(provider='deepseek',client=client).recommend(CATALOG)
                self.assertTrue(result.audit['fallback']);self.assertEqual(result.provider,'template')

    def test_token_budget_rejects_before_http_dispatch(self):
        def reserve(**kwargs):raise RuntimeError('LLM token budget exceeded')
        with patch.dict(os.environ,{'DEEPSEEK_API_KEY':'contract-key'}),patch('openai.OpenAI') as constructor:
            result=advisors.create_advisor(provider='deepseek',budget_controller=SimpleNamespace(reserve=reserve)).recommend(CATALOG)
        constructor.assert_not_called();self.assertTrue(result.audit['fallback'])

    def test_provider_is_validated_in_frozen_spec(self):
        payload={'benchmark':{'config':'benchmark.json'},'providers':['aws_lambda'],'advisor':{'provider':'unknown'}}
        with self.assertRaisesRegex(ValueError,'advisor.provider'):ExperimentSpec.from_dict(payload)
        payload['advisor']['provider']='deepseek'
        self.assertEqual(ExperimentSpec.from_dict(payload).to_dict()['advisor']['provider'],'deepseek')

    def test_missing_usage_is_not_zero_cost(self):
        body=response_body();body.pop('usage')
        client=RealOpenAI(api_key='contract-key',base_url='https://api.deepseek.com',max_retries=0,
            http_client=httpx.Client(transport=httpx.MockTransport(lambda request:httpx.Response(200,json=body))))
        result=advisors.create_advisor(provider='deepseek',client=client,token_price_per_million=1).recommend(CATALOG)
        self.assertEqual(result.audit['usage'],{})
        self.assertIsNone(result.audit['llm_cost_estimate'])

    def test_verified_winner_and_exact_claim_are_accepted_but_override_is_rejected(self):
        metric={'candidate_id':'a','value':2,'unit':'USD','scope':'workflow','currency':'USD'}
        catalog={**CATALOG,'candidates':{'a':{},'b':{}},'eligible_candidates':['a','b'],
            'objective_winner':'a','metrics':{'cost':metric},'decision_status':'recommended'}
        for preferred in ('a','b'):
            with self.subTest(preferred=preferred):
                output={**OUTPUT,'preferred_candidate_id':preferred,'metric_references':['cost'],
                    'metric_claims':[{'metric_id':'cost',**metric}]}
                client=RealOpenAI(api_key='contract-key',base_url='https://api.deepseek.com',max_retries=0,
                    http_client=httpx.Client(transport=httpx.MockTransport(lambda request:httpx.Response(200,json=response_body(output)))))
                result=advisors.create_advisor(provider='deepseek',client=client).recommend(catalog)
                self.assertEqual(result.preferred_candidate_id,'a')
                self.assertEqual(result.audit['fallback'],preferred!='a')
                self.assertEqual(result.provider,'deepseek' if preferred=='a' else 'template')


class AdvisorCheckContracts(unittest.TestCase):
    def check(self,*args):
        stream=io.StringIO()
        with contextlib.redirect_stdout(stream):code=advisor_check.main(list(args))
        return code,json.loads(stream.getvalue())

    def test_configuration_only_never_dispatches_and_missing_key_is_external(self):
        for configured in (False,True):
            with self.subTest(configured=configured),patch.dict(os.environ,{'DEEPSEEK_API_KEY':'contract-key'} if configured else {},clear=True),patch('openai.OpenAI') as constructor:
                code,result=self.check()
                constructor.assert_not_called()
                self.assertEqual(code,0 if configured else 2)
                self.assertFalse(result['real_api_verified'])
                self.assertEqual(result['status'],'configuration_ready' if configured else 'blocked_external')
                self.assertNotIn('contract-key',str(result))

    def test_live_check_uses_real_sdk_and_requires_valid_response_and_observed_usage(self):
        for outcome in ('success','http_error','incomplete','missing_usage','bad_claim','budget'):
            with self.subTest(outcome=outcome):
                requests=[]
                def transport(request):
                    requests.append(request)
                    if outcome=='http_error':return httpx.Response(401,json={'error':{'message':'invalid key'}})
                    output={**OUTPUT,'metric_references':['invented']} if outcome=='bad_claim' else OUTPUT
                    body=response_body(output,'incomplete' if outcome=='incomplete' else 'completed')
                    if outcome=='missing_usage':body.pop('usage')
                    return httpx.Response(200,json=body)
                def constructor(**kwargs):
                    return RealOpenAI(**kwargs,http_client=httpx.Client(transport=httpx.MockTransport(transport)))
                with patch.dict(os.environ,{'DEEPSEEK_API_KEY':'contract-key','ATSUITE_ADVISOR_MODEL':'deepseek-flash'}),patch('openai.OpenAI',side_effect=constructor):
                    code,result=self.check('--live','--max-llm-tokens','1' if outcome=='budget' else '20000')
                self.assertEqual(code,0 if outcome=='success' else 1)
                self.assertEqual(result['real_api_verified'],outcome=='success')
                self.assertEqual(len(requests),0 if outcome=='budget' else 1)
                self.assertNotIn('contract-key',str(result))


class DeepSeekExecutionContracts(unittest.TestCase):
    def experiment(self,root,store,*,enabled=True):
        from atsuite.recommender.service import plan,approve,enqueue
        from tests.test_recommender_candidates import make_config
        config=make_config(root/'bench');raw=json.loads(config.read_text())
        raw['nodes'][0]['state']={'mode':'stateless'}
        raw['correctness']={'oracle':{'type':'exact','expected_output':{'answer':42}}}
        config.write_text(json.dumps(raw))
        spec=ExperimentSpec.from_dict({'benchmark':{'config':str(config)},'providers':['aws_lambda'],
            'regions':{'aws_lambda':'us-east-1'},'objective':{'type':'min_cost'},
            'search':{'mode':'adaptive','max_candidates':2},
            'protocol':{'warmup_runs':1,'exploration_runs':1,'confirmation_runs':3},
            'budget':{'max_estimated_spend':5,'max_wall_time_seconds':120,'max_llm_tokens':100000},
            'advisor':{'enabled':enabled,'provider':'deepseek'}})
        parent=plan(spec,root/'plan'/'plan.json')
        identity={'subject':'local-user','tenant':'local','project':'default'}
        approval=approve(store,parent,**identity)
        return enqueue(store,parent,approval,**identity)

    def test_default_worker_and_web_use_frozen_provider_and_keep_evidence_gate(self):
        from fastapi.testclient import TestClient
        from atsuite.server.app import create_app
        from atsuite.recommender.service import run
        from tests.fake_execution_backend import FakeExecutionBackend
        requests=[]
        def transport(request):
            requests.append(request)
            return httpx.Response(200,json=response_body())
        def constructor(**kwargs):
            return RealOpenAI(**kwargs,http_client=httpx.Client(transport=httpx.MockTransport(transport)))
        with tempfile.TemporaryDirectory() as tmp,patch.dict(os.environ,{'DEEPSEEK_API_KEY':'contract-key','ATSUITE_ADVISOR_MODEL':'deepseek-flash'}),patch('openai.OpenAI',side_effect=constructor):
            root=Path(tmp);backend=FakeExecutionBackend()
            app=create_app(artifact_root=root/'artifacts',execution_backend=backend)
            record=self.experiment(root,app.state.store);frozen=json.loads(json.dumps(record['plan']))
            result=run(app.state.store,record['id'],backend=backend)
            self.assertEqual(result['state'],'COMPLETED',result.get('error'))
            self.assertEqual(result['plan'],frozen)
            self.assertEqual(len(result['trials']),10)
            self.assertEqual(result['explanation']['provider'],'deepseek')
            self.assertFalse(result['explanation']['audit']['fallback'])
            self.assertIsNone(result['recommendation']['best_verified'])
            self.assertFalse(result['comparison']['cloud_verified'])
            self.assertTrue(any(row.get('provider')=='deepseek' for row in result['search_audit']))
            self.assertEqual(len(requests),2)
            with TestClient(app,base_url='http://localhost') as client:
                response=client.post('/api/v1/experiments/'+record['id']+'/advisor',json={'prompt':'Explain available evidence.'})
            self.assertEqual(response.status_code,200,response.text)
            self.assertEqual(response.json()['provider'],'deepseek')
            self.assertFalse(response.json()['audit']['fallback'])
            self.assertEqual(len(requests),3)
            self.assertTrue(all(str(r.url)=='https://api.deepseek.com/responses' for r in requests))
            self.assertNotIn('contract-key',json.dumps(app.state.store.get(record['id'])))
            app.state.store.close()

    def test_disabled_web_ignores_configured_key_and_model(self):
        from fastapi.testclient import TestClient
        from atsuite.server.app import create_app
        with tempfile.TemporaryDirectory() as tmp,patch.dict(os.environ,{'DEEPSEEK_API_KEY':'contract-key','ATSUITE_ADVISOR_MODEL':'deepseek-flash'}),patch('openai.OpenAI') as constructor:
            root=Path(tmp);app=create_app(artifact_root=root/'artifacts')
            record=self.experiment(root,app.state.store,enabled=False)
            with TestClient(app,base_url='http://localhost') as client:
                response=client.post('/api/v1/experiments/'+record['id']+'/advisor',json={})
            self.assertEqual(response.status_code,200,response.text)
            self.assertEqual(response.json()['provider'],'template')
            constructor.assert_not_called();app.state.store.close()
