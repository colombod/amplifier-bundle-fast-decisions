#!/usr/bin/env python3
"""Run bounded four-arm blocks, audit receipts, grade, prune, and refresh the website."""
from __future__ import annotations
import argparse
import fcntl
import json
from pathlib import Path
import subprocess
import sys

import build_site
import forge_swebench as campaign


def audit(root, manifest, names):
    failures = []
    for name in names:
        run = root/'runs'/name
        result = json.loads((run/'result.json').read_text())
        item = manifest['runs'][name]
        expected_mode = 'active' if manifest['arms'][item['arm']].get('active') else 'off'
        native = result.get('native') or {}
        if result.get('infrastructure_failure') or result.get('cost_usd') is None:
            failures.append({'run':name,'reason':'infrastructure_or_unknown_cost'})
        if any(native.get('tool_names',{}).get(tool,0) for tool in ['delegate','task','spawn_agent','agent','recipes']):
            failures.append({'run':name,'reason':'delegation_would_escape_parent_cost_accounting'})
        models = result.get('models_served') or []
        if not models or any(row.get('model') != item['model'] or row.get('thinking_enabled') is not True for row in models):
            failures.append({'run':name,'reason':'served_model_or_thinking_mismatch'})
        sources = []
        backend_errors = 0
        for path in (run/'events').glob('*.jsonl'):
            for line in path.read_text().splitlines():
                event=json.loads(line)
                if event.get('session_id') != result.get('session_id'):
                    continue
                data=event.get('data') or {}
                if event.get('event')=='fast_decisions:source':
                    sources.append(data)
                if data.get('reason_code')=='backend_error':
                    backend_errors += 1
        if not sources or any(s.get('source_tree_sha256') != manifest['source_tree_sha256']
                              or s.get('mode') != expected_mode for s in sources):
            failures.append({'run':name,'reason':'runtime_source_or_mode_mismatch'})
        if backend_errors:
            failures.append({'run':name,'reason':'judge_backend_error','count':backend_errors})
    return failures


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root',type=Path,required=True)
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--max-cost-usd',type=float,required=True)
    args=parser.parse_args()
    root=args.root.resolve()
    with (root/'supervisor.lock').open('a') as lock:
        try: fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError: raise SystemExit('Another supervisor already owns this campaign')
        manifest=json.loads((root/'manifest.json').read_text())
        # Inspect existing runs before resuming; never silently discard a failed attempt.
        existing=[name for name in manifest['run_order'] if (root/'runs'/name/'result.json').exists()]
        failures=audit(root,manifest,existing)
        if failures:
            campaign._dump(root/'audit-failures.json',failures)
            campaign._dump(root/'campaign-status.json',{'status':'audit_failed','cap_usd':args.max_cost_usd})
            build_site.build(root,args.output)
            raise SystemExit('Existing receipts failed the campaign audit')
        while True:
            pending=[name for name in manifest['run_order'] if not (root/'runs'/name/'result.json').exists()]
            if not pending:
                rows=campaign._report_rows(root,manifest)
                complete=len(rows)==len(manifest['runs']) and all(r['resolved'] is not None for r in rows)
                campaign._dump(root/'campaign-status.json',{'status':'complete' if complete else 'grading_error',
                    'cap_usd':args.max_cost_usd,'spent_usd':manifest.get('prior_cost_usd',0)+sum(r['cost_usd'] for r in rows)})
                campaign.cmd_report(argparse.Namespace(root=str(root),quiet=True))
                build_site.build(root,args.output)
                return
            iid=manifest['runs'][pending[0]]['instance_id']
            block=[n for n in pending if manifest['runs'][n]['instance_id']==iid]
            # The frozen schedule groups all arms of an issue. One serial controller
            # enforces the global cost guard; no parallel workers bypass reservations.
            if pending[:len(block)]!=block:
                raise SystemExit('This supervisor requires contiguous issue blocks')
            argv=[sys.executable,str(Path(campaign.__file__).resolve()),'run','--root',str(root),
                  '--parallel','1','--max-cost-usd',str(args.max_cost_usd),'--max-runs',str(len(block)),
                  '--grade-blocks','--prune-graded-images']
            with (root/'controller.log').open('a') as log:
                result=subprocess.run(argv,stdout=log,stderr=log)
            status=json.loads((root/'campaign-status.json').read_text())
            if result.returncode or status.get('status')=='budget_exhausted':
                if result.returncode and status.get('status') in {'running','grading'}:
                    campaign._dump(root/'campaign-status.json',{'status':'needs_reconciliation',
                        'cap_usd':args.max_cost_usd,'instance_id':iid})
                build_site.build(root,args.output)
                raise SystemExit(result.returncode)
            failures=audit(root,manifest,block)
            if failures:
                campaign._dump(root/'audit-failures.json',failures)
                campaign._dump(root/'campaign-status.json',{'status':'audit_failed','cap_usd':args.max_cost_usd})
                build_site.build(root,args.output)
                raise SystemExit('New receipts failed the campaign audit')
            campaign._dump(root/'last-audit.json',{'passed':True,'runs':block,'instance_id':iid})
            build_site.build(root,args.output)
            print(json.dumps({'audited':iid,'remaining':len(pending)-len(block)}),flush=True)


if __name__=='__main__':main()
