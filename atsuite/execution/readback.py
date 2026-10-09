"""Readback keeps tunable, derived and provider-managed fields distinct."""
from atsuite.recommender.identity import digest

def configuration_from_map(candidate,published,payload):
    expected=set(candidate['resources_by_target']); targets=payload.get('targets',{})
    if expected != set(targets): raise RuntimeError('deployment endpoint coverage mismatch')
    effective={};observed_images={};requested_images={};limitations=[]
    for name in sorted(expected):
        entry=targets[name];raw=entry.get('readback') or {};fields=raw.get('effective_resources') or entry.get('effective_resources') or {}
        requested=entry.get('requested_resources') or {}
        semantics=raw.get('field_semantics') or {}
        required={'memory','timeout'} if candidate['provider']=='aws_lambda' else {'cpu','memory','timeout'}
        if candidate['provider']=='aws_agentcore': required=set()
        for field in required:
            if field not in fields: limitations.append(f'{name}:missing_effective:{field}')
            elif field in requested and fields[field]!=requested[field] and semantics.get(field) not in {'derived','provider_managed'}:
                raise RuntimeError('configuration_drift:'+name+':'+field)
        for field in ('concurrency','architecture','billing_mode'):
            architecture_rules={'platform_fixed_amd64':'linux/amd64','platform_fixed_arm64':'linux/arm64'}
            if field=='architecture' and semantics.get(field) in architecture_rules:
                if architecture_rules[semantics[field]]!=candidate['runtime_architecture'] or published[name].get('platform')!=candidate['runtime_architecture']:
                    limitations.append(f'{name}:architecture_rule_mismatch')
                continue
            if requested.get(field) is not None and semantics.get(field) not in {'provider_managed','derived'}:
                if fields.get(field)!=requested[field]: limitations.append(f'{name}:unverified:{field}')
        actual_region=raw.get('effective_region')
        if actual_region!=candidate['region']: limitations.append(f'{name}:region_mismatch')
        if raw.get('runtime_status') not in {'READY','Active','Ready','RUNNING','Running'}: limitations.append(f'{name}:not_ready')
        requested_images[name]=published[name]['digest']
        observed_images[name]=raw.get('image_digest','')
        if requested_images[name]!=observed_images[name]: limitations.append(f'{name}:image_mismatch')
        effective[name]=fields
    image='sha256:'+digest(requested_images,length=64)
    actual='sha256:'+digest(observed_images,length=64)
    return {'status':'verified' if not limitations else 'unverified','image_digest':image,'effective_image_digest':actual,
        'requested_config':candidate['resources_by_target'],'effective_config':effective,'readback_limitations':limitations,
        'targets':targets,'region':candidate['region']}
