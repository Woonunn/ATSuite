"""Fetch actual vendor billing snapshots; daily account totals are never window costs."""
import argparse
from datetime import datetime,timezone,timedelta
import json
from pathlib import Path
from dotenv import load_dotenv
from experiments.exp1.data.prepare_e1a import ROOT


def fetch_aws(day,output):
    import boto3
    date=datetime.fromisoformat(day).date()
    response=boto3.client('ce',region_name='us-east-1').get_cost_and_usage(
        TimePeriod={'Start':str(date),'End':str(date+timedelta(days=1))},Granularity='DAILY',
        Metrics=['UnblendedCost','UsageQuantity'],GroupBy=[{'Type':'DIMENSION','Key':'SERVICE'}])
    periods=response.get('ResultsByTime',[])
    estimated=not periods or any(period.get('Estimated') is not False for period in periods)
    result={'basis':'vendor_estimated_billing' if estimated else 'actual_vendor_billing','scope':'account-wide daily service totals; not attributable to an E1-A window',
            'attribution_status':'unallocated','queried_utc':datetime.now(timezone.utc).isoformat(),'raw':response}
    Path(output).write_text(json.dumps(result,indent=2,default=str)+'\n');return result


def fetch_ali(day,output,instance_id=None):
    from alibabacloud_tea_openapi.client import Client
    from alibabacloud_tea_openapi import models
    from alibabacloud_credentials.client import Client as Credentials
    from alibabacloud_tea_util.models import RuntimeOptions
    date=datetime.fromisoformat(day).date()
    client=Client(models.Config(credential=Credentials(),endpoint='business.aliyuncs.com'))
    params=models.Params(action='DescribeInstanceBill',version='2017-12-14',protocol='HTTPS',pathname='/',method='POST',auth_type='AK',body_type='json',req_body_type='formData',style='RPC')
    pages=[];token=None
    while True:
        query={'BillingCycle':date.strftime('%Y-%m'),'Granularity':'DAILY','BillingDate':str(date),'MaxResults':'300'}
        if token:query['NextToken']=token
        if instance_id:query['InstanceID']=instance_id
        response=client.call_api(params,models.OpenApiRequest(query=query),RuntimeOptions(connect_timeout=8000,read_timeout=20000))
        body=response.get('body',{})
        if not body.get('Success'):raise RuntimeError('Billing API failed: '+str(body.get('Code')))
        pages.append(response);next_token=body.get('Data',{}).get('NextToken')
        if not next_token:break
        if next_token==token:raise RuntimeError('Billing pagination did not advance')
        token=next_token
    result={'basis':'actual_vendor_billing','scope':'daily instance bill' if instance_id else 'daily account bill',
            'attribution_status':'requires usage-backed allocation to experiment windows',
            'queried_utc':datetime.now(timezone.utc).isoformat(),'instance_id':instance_id,'pages':pages}
    Path(output).write_text(json.dumps(result,indent=2,default=str)+'\n');return result


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--provider',choices=['aws','ali'],required=True);p.add_argument('--day',required=True)
    p.add_argument('--instance-id');p.add_argument('--output',required=True,type=Path)
    a=p.parse_args();load_dotenv(ROOT/'.env');a.output.parent.mkdir(parents=True,exist_ok=True)
    if a.output.exists():raise FileExistsError('Keep each original vendor billing snapshot')
    result=fetch_aws(a.day,a.output) if a.provider=='aws' else fetch_ali(a.day,a.output,a.instance_id)
    print(json.dumps({k:v for k,v in result.items() if k not in {'raw','pages'}},indent=2))
