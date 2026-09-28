"""Export metadata-only measurements from the completed decision-alternatives study."""
import argparse, collections, json, statistics
from pathlib import Path

ap=argparse.ArgumentParser(description=__doc__)
ap.add_argument('study',type=Path);ap.add_argument('--output',type=Path,required=True)
a=ap.parse_args();manifest=json.loads((a.study/'manifest.json').read_text());rows=[]
for name in manifest['run_order']:
 p=a.study/name;r=json.loads((p/'result.json').read_text());n=r['native'];totals=r['measurements']['totals']
 scored=[];failures=0
 for receipt in p.glob('events/*.jsonl'):
  for line in receipt.open():
   e=json.loads(line)
   if e.get('session_id')!=r['session_id']:continue
   if e['event']=='fast_decisions:scored':scored.append(e['data'])
   if e['event']=='fast_decisions:fallback' and e['data'].get('reason_code') in {'backend_error','decision_timeout'}:failures+=1
 jev=[e for e in scored if e.get('backend')=='jev']
 judge_cost=sum(e['input_tokens']*.042/1e6 for e in jev) if all(isinstance(e.get('input_tokens'),int) for e in jev) and not failures else None
 provider_cost=n['usage']['cost_usd']
 total=None if r['side']=='retrieval' or judge_cost is None or provider_cost is None else provider_cost+judge_cost
 rows.append({'name':name,'task':r['task'],'arm':r['side'],'passed':r['outcome_passed'],
  'checks':r['quality'],'wall_seconds':r['wall_time_ms']/1000,'provider_calls':n['provider_responses'],
  'tool_calls':n['tool_results'],'tool_names':n['tool_names'],'calls_bypassed':totals['provider_calls_bypassed'],
  'judge_scored':len(scored),'judge_failures':failures,'provider_cost_usd':provider_cost,
  'judge_cost_usd':judge_cost,'total_estimated_cost_usd':total,'source_match':r['source_match'],
  'mode_match':r['mode_match'],'session_id':r['session_id'],'source_hash':r['source_expected']['tree_sha256'],
  'fallbacks':totals['fallback_reasons'],'prompt_sha256':manifest['runs'][name]['prompt_sha256'],
  'workspace_hash':manifest['runs'][name]['workspace_hash'],'profile_sha256':manifest['runs'][name]['profile_sha256']})
groups=collections.defaultdict(list)
for row in rows:groups[(row['task'],row['arm'])].append(row)
summary=[]
for (task,arm),group in groups.items():
 costs=[r['total_estimated_cost_usd'] for r in group]
 summary.append({'task':task,'arm':arm,'passed':sum(r['passed'] for r in group),'runs':len(group),
  'mean_seconds':statistics.mean(r['wall_seconds'] for r in group),
  'mean_provider_calls':statistics.mean(r['provider_calls'] for r in group),
  'mean_provider_cost_usd':statistics.mean(r['provider_cost_usd'] for r in group),
  'mean_total_estimated_cost_usd':statistics.mean(costs) if all(v is not None for v in costs) else None,
  'calls_bypassed':sum(r['calls_bypassed'] for r in group)})
a.output.parent.mkdir(parents=True,exist_ok=True)
a.output.write_text(json.dumps({'schema':'decision-alternatives-v1','model':manifest['model'],
 'source_hash':manifest['sides']['jev']['source_tree_sha256'],'rows':rows,'summary':summary,
 'limits':['2 repetitions per task/arm; illustrative public fixtures, not a general benchmark.',
 'Jevgrep charges unavailable; retrieval total cost remains null.',
 'Local model startup excluded; provider costs are estimates, not invoices.']},indent=2)+'\n')
print(json.dumps(summary,indent=2))
