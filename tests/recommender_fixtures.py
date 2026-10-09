from atsuite.recommender.evidence_adapter import candidate_evidence

def verified_candidate(cid,cost=.1,count=300,duration=100,source='platform_observed',memory=None):
    candidate={'candidate_id':cid,'workload_fingerprint':'w','bundle_digest':'bundle','config_digest':'config'}
    image='sha256:'+'a'*64
    trials=[{'candidate_id':cid,'scenario_id':'s1','workload_fingerprint':'w','phase':'confirmation','run_id':f'{cid}-run-{i}',
        'attempt_id':f'{cid}-attempt-{i}','block_id':f'confirmation-{i}','status':'success','output_validation':'passed',
        'state_validation':'passed','duration_ms':duration,'image_digest':image,'memory_mb':memory} for i in range(count)]
    config={'status':'verified','image_digest':image,'effective_image_digest':image,'state_validation':'passed'}
    evidence=candidate_evidence(candidate,trials,configuration=config,cost={'amount':cost*count,'currency':'USD','coverage':'complete'},
                              ledger=[{'quantity':'1'}],rate_snapshot={'snapshot_id':'rate'},oracle_version='oracle-v1',scenario_id='s1',measurement_source=source)
    # Explicit unit-test contract: fixed per-attempt price, no stochastic usage.
    evidence['metrics']['cost'].update(ci_lower=cost,ci_upper=cost)
    return evidence
