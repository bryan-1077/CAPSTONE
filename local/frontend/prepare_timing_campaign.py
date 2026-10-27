#!/usr/bin/env python3
"""Prepare immutable timing requests/acceptance plans. Never calls an LLM."""
import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import uuid
import yaml

from architecture import ROOT, fingerprint
from campaigns.timing import VERSION, cases
from generate_specification import generation_context
from planning_library import planning_catalog
from specification_request import build_specification_request


def prepare(output):
    output = Path(output).resolve()
    definitions = cases()
    catalog = planning_catalog()
    context = generation_context(catalog=catalog)
    # Validate all request conversions before creating a campaign directory.
    requests = [build_specification_request(case['config']) for case in definitions]
    output.mkdir(parents=True, exist_ok=False)
    entries = []
    for case, request in zip(definitions, requests):
        folder = output / 'cases' / case['id']
        folder.mkdir(parents=True)
        (folder/'request.yaml').write_text(yaml.safe_dump(case['config'],sort_keys=False))
        (folder/'description.txt').write_text(case['config']['requirements'][0]+'\n')
        entry = {k:v for k,v in case.items() if k != 'config'}
        entry.update(request='cases/'+case['id']+'/request.yaml',request_fingerprint=fingerprint(request),
                     generation_status='not_run',review_status='not_run',lint_status='not_run',
                     behavioral_status='not_run',llm_calls=0,cost_usd=0)
        entries.append(entry)
    manifest = {'campaign_version':VERSION,'family':'timing','created_at':datetime.now(timezone.utc).isoformat(),
                'catalog_fingerprint':catalog['catalog_fingerprint'],
                'planning_context_fingerprint':fingerprint(context),'status':'prepared_not_generated',
                'result_policy':'Preparation, schema/lint/review success and checker availability are not behavioral PASS. '
                                'Each paid trial needs a fresh build, request/model/context identity, usage and independent evidence.',
                'cases':entries}
    manifest['definition_fingerprint'] = fingerprint({'version':VERSION,'definitions':definitions,
                                                      'catalog':catalog['catalog_fingerprint'],'context':fingerprint(context)})
    (output/'campaign.json').write_text(json.dumps(manifest,indent=2)+'\n')
    (output/'planning_context.json').write_text(json.dumps(context,indent=2)+'\n')
    (output/'library_catalog.json').write_text(json.dumps(catalog,indent=2)+'\n')
    return output/'campaign.json'


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output-dir',type=Path)
    args=parser.parse_args()
    output=args.output_dir or ROOT/'microarch/campaigns/timing'/(
        datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')+'_'+uuid.uuid4().hex[:8])
    print(prepare(output))


if __name__=='__main__':main()
