"""Check local Advisor configuration; --live explicitly enables one model call.

No cloud resources, workload code or invented benchmark measurements are sent.
"""
from __future__ import annotations
import argparse
import json
import os
from atsuite.advisor.client import create_advisor


class TokenBudget:
    def __init__(self, limit):
        self.limit=limit
        self.reserved=0

    def reserve(self, *, llm_tokens, **kwargs):
        if self.reserved+llm_tokens>self.limit:
            raise RuntimeError('LLM token budget exceeded')
        self.reserved+=llm_tokens


def main(argv=None):
    from atsuite.config import load_local_env
    load_local_env()
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--provider',choices=('deepseek','openai'),default='deepseek')
    parser.add_argument('--live',action='store_true',help='approve one potentially billable real model call')
    parser.add_argument('--max-llm-tokens',type=int,default=8192)
    parser.add_argument('--max-output-tokens',type=int,default=512)
    args=parser.parse_args(argv)
    if args.max_llm_tokens<=0 or not 1<=args.max_output_tokens<=4096:
        parser.error('positive token budget and max-output-tokens between 1 and 4096 required')
    key_env='DEEPSEEK_API_KEY' if args.provider=='deepseek' else 'OPENAI_API_KEY'
    model=os.environ.get('ATSUITE_ADVISOR_MODEL') or ('deepseek-flash' if args.provider=='deepseek' else '')
    missing=[name for name,value in ((key_env,os.environ.get(key_env,'')),('ATSUITE_ADVISOR_MODEL',model)) if not value.strip()]
    summary={'provider':args.provider,'model':model,'api_key_configured':key_env not in missing,
        'missing_configuration':missing,'real_api_verified':False,
        'status':'blocked_external' if missing else 'configuration_ready'}
    if missing or not args.live:
        print(json.dumps(summary,ensure_ascii=False));return 2 if missing else 0
    budget=TokenBudget(args.max_llm_tokens)
    advisor=create_advisor(provider=args.provider,model=model,budget_controller=budget,max_output_tokens=args.max_output_tokens)
    catalog={'candidates':{},'eligible_candidates':[],'objective_winner':None,'metrics':{},'decision_status':'insufficient_evidence'}
    response=advisor.recommend(catalog,'Explain that this is a connectivity check with no verified experiment evidence. Return no proposals, no preferred candidate and no metric claims.')
    audit=response.audit
    # A successful template fallback is not proof that the model API worked.
    usage=audit.get('usage') or {}
    observed_usage=all(isinstance(usage.get(name),int) and not isinstance(usage[name],bool) and usage[name]>=0 for name in ('input_tokens','output_tokens','total_tokens'))
    accepted=response.provider==args.provider and response.valid and audit.get('fallback') is False and bool(audit.get('response_id')) and observed_usage
    summary.update(status='real_api_verified' if accepted else 'connection_failed',real_api_verified=accepted,
        response_id=audit.get('response_id'),fallback=audit.get('fallback'),error=audit.get('error'),
        validation_errors=list(response.validation_errors),usage=usage,reserved_tokens=budget.reserved)
    print(json.dumps(summary,ensure_ascii=False));return 0 if accepted else 1


if __name__=='__main__':raise SystemExit(main())
