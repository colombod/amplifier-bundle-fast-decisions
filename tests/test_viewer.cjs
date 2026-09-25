const {test} = require('node:test');
const assert = require('node:assert/strict');
const {scriptedKeys,synthetic,sessionsFor,metrics,describe,valid,decisionPath,summarize,sessionName,circuitFor} = require('../src/amplifier_fast_decisions/static/app.js');
let seq=0;
const now=Date.parse('2026-09-18T03:00:00Z');
const event=(kind,data={},decision=null,session='parent',extra={})=>({schema_version:'1.0',event_id:String(++seq),event:'fast_decisions:'+kind,session_id:session,decision_id:decision,timestamp:new Date(now).toISOString(),data,...extra});
function real(events){const keys=scriptedKeys(events);return events.filter(e=>!synthetic(e,keys));}
test('scripted decision chains never count as model decisions or fast-path gains',()=>{
 const events=[event('health',{phase:'configuration',backend:'scripted-demo',mode:'shadow'}),event('requested',{},'d1'),event('shadow_proposed',{synthetic:true,backend:'scripted-demo'},'d1'),event('shadow_agreement',{agreement:'match',would_have_avoided_llm_turn:true},'d1'),event('health',{native_event:'tool:post'},'d1')];
 assert.equal(real(events).length,2);assert.equal(metrics(real(events)).scores,0);assert.equal(metrics(real(events)).tools,1);assert.equal(metrics(real(events)).fastExecuted,0);
});
test('native and measured tool events do not double count within a session',()=>{
 const events=[event('health',{native_event:'tool:post'}),event('tool_end',{status:'ok'}),event('tool_end',{status:'ok'},null,'other')];
 assert.equal(metrics(events).tools,2);
});
test('fast submission alone never proves an executed action',()=>{
 const events=[event('routed',{route:'fast'},'d1')];assert.equal(metrics(events).fast,1);assert.equal(metrics(events).fastExecuted,0);
 events.push(event('tool_end',{status:'ok'},'d1'));assert.equal(metrics(events).fastExecuted,1);
});
test('decision ids cannot link execution across sessions',()=>{
 const events=[event('routed',{route:'fast'},'same','one'),event('tool_end',{status:'ok'},'same','two')];
 assert.equal(metrics(events).fastExecuted,0);
});
test('synthetic flag on an entire fixture session excludes native-looking data',()=>{
 const e=event('health',{native_event:'tool:post'},null,'demo',{synthetic:true});assert.equal(real([e]).length,0);
});
test('children retain their parent identity; parents remain independently selectable',()=>{
 const events=[event('health',{phase:'configuration',backend:'ollama-token',mode:'active'}),event('health',{phase:'configuration'},null,'child',{parent_session_id:'parent'})];
 const sessions=sessionsFor(events,now);assert.equal(sessions.filter(s=>!s.parent).length,1);assert.equal(sessions.find(s=>s.id==='child').parent,'parent');
});
test('connected viewer does not make old sessions appear alive',()=>{
 const e=event('health',{phase:'configuration'});assert.equal(sessionsFor([e],now+60000)[0].reporting,false);
 const recentNative=event('health',{native_event:'provider:request'});assert.equal(sessionsFor([recentNative],now)[0].reporting,false);
});
test('heartbeats distinguish mounted idle sessions from working sessions and closed sessions',()=>{
 const events=[event('health',{phase:'session_heartbeat'})];assert.equal(sessionsFor(events,now)[0].state,'Connected, idle');
 events.push(event('health',{native_event:'execution:start'}));assert.equal(sessionsFor(events,now)[0].state,'Working');
 events.push(event('health',{native_event:'execution:end'}));assert.equal(sessionsFor(events,now)[0].state,'Connected, idle');
 events.push(event('health',{phase:'session_closed'}));assert.equal(sessionsFor(events,now)[0].reporting,false);
});
test('retry events explain real provider issues without error bodies',()=>{
 const e=event('health',{native_event:'provider:retry',provider:'anthropic',retry_attempt:2,exception_type:'ConnectTimeout'});
 assert.match(describe(e).detail,/anthropic.*ConnectTimeout.*Attempt 2/);assert.equal(metrics([e]).issues,1);
});
test('local model scores are model evidence; scripted scores remain explicit',()=>{
 const e=event('scored',{model:'qwen3:0.6b',duration_ms:24,probability_kind:'token_mass_with_abstention_residual'});
 assert.equal(describe(e).source,'Model score');assert.equal(describe(e,true).source,'Synthetic / scripted');assert.equal(metrics([e]).p95,24);
});
test('malformed imported data is rejected',()=>{
 assert.equal(valid({...event('health'),data:[]}),false);assert.equal(valid(event('health')),true);
});

test('mechanics joins only the selected session and decision, without inventing missing stages',()=>{
 const scored=event('scored',{model:'qwen3:0.6b'},'same','one');
 const other=event('tool_end',{status:'ok'},'same','two');
 const next=event('scored',{},'later','one');
 const events=[scored,other,next];
 assert.deepEqual(decisionPath(events,'one',scored.event_id),[scored]);
 assert.deepEqual(decisionPath(events,'one',null),[next]);
 assert.deepEqual(decisionPath(events,'two',null),[]);
});

test('bypassed calls require instrumented submission; advisory scores never imply savings',()=>{
 const events=[event('scored',{mode:'advisory'},'advice'),event('routed',{route:'fast'},'incomplete')];
 assert.equal(metrics(events).bypassed,0);
 events.push(event('routed',{route:'fast',status:'submitted_to_upstream'},'actual'));
 assert.equal(metrics(events).bypassed,1);assert.equal(metrics(events).fastExecuted,0);
});

test('ledger requires explicit completion and submission evidence',()=>{
 const route=event('routed',{route:'fast'},'d');
 assert.equal(summarize([route]).verdict.label,'Fast · selected');
 assert.match(summarize([route]).verdict.note,/Bypass not confirmed/);
 route.data.status='submitted_to_upstream';
 assert.equal(summarize([route]).verdict.label,'Fast · submitted');
 const cancelled=event('tool_end',{status:'cancelled'},'d');
 assert.equal(summarize([route,cancelled]).verdict.label,'Fast · cancelled');
 assert.doesNotMatch(summarize([route,cancelled]).happened.detail,/executed/);
 const slow=event('routed',{route:'slow'},'slow');
 assert.doesNotMatch(summarize([slow]).verdict.note,/provider answered/);
 assert.match(summarize([slow,event('slow_end',{status:'ok'},'slow')]).verdict.note,/provider answered/);
});
test('session labels prefer explicit names and recover after retries',()=>{
 assert.equal(sessionName({workspace_name:'repo'},'abcdefghi'),'repo');
 assert.equal(sessionName({workspace_name:'repo',session_label:'Review'},'id'),'Review');
 const rows=[event('health',{phase:'configuration'}),event('health',{native_event:'provider:retry'}),event('health',{native_event:'tool:pre'})];
 assert.equal(sessionsFor(rows,now)[0].state,'Working');
});

test('circuit never turns a score, shadow agreement, or advisory suggestion into a fast route',()=>{
 const score=event('scored',{choice:'read',selected_probability:.99},'d');
 assert.equal(circuitFor([score]).branch,'unknown');
 const route=event('routed',{route:'fast',status:'submitted_to_upstream'},'d');
 assert.equal(circuitFor([score,route]).branch,'fast');
 assert.equal(circuitFor([score,route]).receipt,route);
 assert.equal(summarize([score,route]).verdict.label,'Fast · submitted');
 const shadow=event('shadow_proposed',{choice:'read'},'d');
 assert.equal(circuitFor([shadow,route,event('shadow_agreement',{agreement:'match'},'d')]).branch,'unknown');
 assert.equal(circuitFor([event('scored',{mode:'advisory',choice:'read'},'d'),route]).branch,'unknown');
});

test('circuit distinguishes a recorded provider route from an invocation and preserves failed outcomes',()=>{
 const route=event('routed',{route:'slow'},'d');
 assert.equal(circuitFor([route]).slow,route);
 const started=event('slow_start',{provider:'test-provider'},'d');
 assert.equal(circuitFor([route,started]).slow,started);
 const failed=event('tool_end',{status:'error',success:false},'d');
 const fast=event('routed',{route:'fast',status:'submitted_to_upstream'},'d');
 assert.equal(circuitFor([fast,failed]).receipt,failed);
 assert.equal(summarize([fast,failed]).verdict.label,'Fast · failed');
 assert.equal(circuitFor([]).branch,'unknown');
 assert.equal(circuitFor([]).receipt,undefined);
 const observed=event('health',{native_event:'tool:post',tool_call_id:'native-tool',tool:'read_file'});
 assert.equal(circuitFor([observed]).host,observed);
 assert.equal(circuitFor([observed]).branch,'unknown');
});
test('configured backend label names the decision-maker',()=>{
 const cfg=event('health',{phase:'configuration',backend:'jev',backend_label:'Other scorer',mode:'active'});
 assert.match(describe(cfg).detail,/^Other scorer · active/);
 assert.match(describe(event('health',{phase:'configuration',backend:'jev',mode:'active'})).detail,/^Jev · active/);
});
test('savings view explains empty and estimated states',()=>{
 const {savingsView}=require('../src/amplifier_fast_decisions/static/app.js');
 assert.equal(savingsView({turns:{total:0}}).cost,'—');
 const v=savingsView({files:3,host_model:'claude-fable-5-1',turns:{total:10,cheap:7,strong:3,judge_calls:10},cost:{saved_usd:12.5,cheap_turns_actual_usd:5,cheap_turns_on_host_usd:17.5,requests_without_cache_data:2},time:{available:false}});
 assert.equal(v.cost,'$12.50');assert.equal(v.turns,'7 of 10');assert.equal(v.time,'Not yet');assert.match(v.note,/2 older requests/);
});
test('negative savings read as costing more',()=>{
 const {savingsView}=require('../src/amplifier_fast_decisions/static/app.js');
 const v=savingsView({files:1,host_model:'claude-opus-5-5',turns:{total:1,cheap:1,strong:0},cost:{saved_usd:-0.1,cheap_turns_actual_usd:0.3,cheap_turns_on_host_usd:0.2},time:{available:false}});
 assert.equal(v.cost,'−$0.10 (costs more)');
});
test('savings time shows the time saved, not the time spent',()=>{
 const {savingsView}=require('../src/amplifier_fast_decisions/static/app.js');
 const v=savingsView({files:1,host_model:'claude-opus-5-5',turns:{total:4,cheap:4,strong:0},cost:{saved_usd:1},time:{available:true,saved_seconds:600,cheap_turns_model_seconds:1200,cheap_turns_on_host_seconds:1800}});
 assert.equal(v.time,'10 min');assert.equal(v.timeDetail,"20 min on the faster model vs 30 min at your usual model's measured speed");
});
test('provider calls show which model ran and why',()=>{
 const {summarize}=require('../src/amplifier_fast_decisions/static/app.js');
 const j=event('difficulty_judged',{backend:'jev',choice:'cheap',reason_code:'judge_cheap',probabilities:{complex:0.01}},'d1','s',{turn_id:'t1'});
 const cheap=[j,event('model_routed',{reason_code:'start_model',requested_model:'claude-sonnet-5'},'d1','s',{turn_id:'t1'}),event('routed',{route:'slow',reason_code:'read_shortcut_disabled'},'d1','s',{turn_id:'t1'}),event('slow_end',{status:'ok',model:'claude-sonnet-5',host_model:'claude-opus-5-5',duration_ms:900},'d1','s',{turn_id:'t1'})];
 const a=summarize(cheap,false,{});
 assert.equal(a.verdict.label,'Faster model');assert.match(a.happened.title,/Faster model · claude-sonnet-5/);assert.equal(a.proposed.title,'Easy → faster model');assert.match(a.proposed.detail,/1% hard/);
 const up=[event('model_routed',{reason_code:'escalated_max_requests'},'d2','s'),event('routed',{route:'slow'},'d2','s'),event('slow_end',{status:'ok',model:'provider-default',host_model:'claude-opus-5-5'},'d2','s')];
 const b=summarize(up,false,{});assert.equal(b.verdict.label,'Switched up');assert.match(b.happened.title,/Usual model · claude-opus-5-5/);
 const big=[event('difficulty_judged',{backend:'jev',choice:'strong',reason_code:'scope_strong',candidate_count:314},'d3','s'),event('model_routed',{reason_code:'start_strong'},'d3','s'),event('routed',{route:'slow'},'d3','s'),event('slow_end',{status:'ok',model:'provider-default',host_model:'claude-opus-5-5'},'d3','s')];
 const c=summarize(big,false,{});assert.equal(c.verdict.label,'Usual model');assert.match(c.proposed.detail,/Large project.*314 files/);
});
test('sessions are named by repo and show their harness',()=>{
 const {sessionName,harnessOf}=require('../src/amplifier_fast_decisions/static/app.js');
 assert.equal(sessionName({repo:'amplifier-bundle-fast-decisions',subdir:'src',workspace_name:'src'},'abcdef123'),'amplifier-bundle-fast-decisions/src');
 assert.equal(sessionName({workspace_name:'tmp'},'abcdef123'),'tmp');
 assert.equal(harnessOf({harness:'Amplifier TUI'}),'Amplifier TUI');
 assert.equal(harnessOf({engine:'codex'}),'Codex');
});
test('scoring column shows who decided each request',()=>{
 const {summarize}=require('../src/amplifier_fast_decisions/static/app.js');
 const judged=[event('difficulty_judged',{backend:'jev',choice:'cheap',reason_code:'judge_cheap',probabilities:{complex:0.12},duration_ms:140},'d1'),event('routed',{route:'slow'},'d1')];
 const a=summarize(judged,false,{}).scoring; assert.match(a.label,/Jev · 12% hard/); assert.notEqual(a.value,'—');
 const big=[event('difficulty_judged',{backend:'jev',choice:'strong',reason_code:'scope_strong',candidate_count:326},'d2'),event('routed',{route:'slow'},'d2')];
 const b=summarize(big,false,{}).scoring; assert.equal(b.value,'rule'); assert.match(b.label,/large project/);
 const plain=[event('routed',{route:'slow'},'d3')]; assert.equal(summarize(plain,false,{}).scoring.label,'no score');
});
test('negative time saved reads as slower',()=>{
 const {savingsView}=require('../src/amplifier_fast_decisions/static/app.js');
 const v=savingsView({files:1,host_model:'claude-opus-5-5',turns:{total:2,cheap:1,strong:1},cost:{saved_usd:1,cheap_turns_actual_usd:1,cheap_turns_on_host_usd:2},time:{available:true,saved_seconds:-9,cheap_turns_model_seconds:46,cheap_turns_on_host_seconds:37}});
 assert.match(v.time,/^−9 s \(slower\)$/); assert.match(v.timeDetail,/wrote more slowly/);
});
test('loop engine names are not shown as a harness',()=>{
 const {harnessOf}=require('../src/amplifier_fast_decisions/static/app.js');
 assert.equal(harnessOf({engine:'upstream-loop-streaming'}),'');
 assert.equal(harnessOf({engine:'claude'}),'Claude Code');
});
test('savings panel explains why totals are flat',()=>{
 const {savingsView}=require('../src/amplifier_fast_decisions/static/app.js');
 const v=savingsView({files:1,host_model:'claude-opus-5-5',turns:{total:5,cheap:1,strong:4},cost:{saved_usd:1,cheap_turns_actual_usd:1,cheap_turns_on_host_usd:2},time:{available:false},recent:{last_cheap_turn_at:new Date(Date.now()-3*3600e3).toISOString(),turns_since:4,turns_since_by_reason:{scope_strong:4}}});
 assert.match(v.note,/Last request on the faster model 3h ago\. Since then 4 requests stayed on your usual model \(4 in large projects/);
});
test('savings names each session usual model and lists projects',()=>{
 const {savingsView}=require('../src/amplifier_fast_decisions/static/app.js');
 const v=savingsView({files:3,host_model:'claude-sonnet-5',cheap_turn_hosts:{'claude-opus-5-5':10,'claude-haiku-4-5-20251001':5},turns:{total:3,cheap:2,strong:1},cost:{saved_usd:1,cheap_turns_actual_usd:1,cheap_turns_on_host_usd:2},time:{available:false},by_project:[{project:'teaserkit',sessions:2,cheap_turns:2,strong_turns:1,saved_usd:-2.46,by_reason:{}},{project:'big-repo',sessions:1,cheap_turns:0,strong_turns:4,saved_usd:0,by_reason:{scope_strong:4}}]});
 assert.match(v.costDetail,/each session's usual model \(opus-5-5, haiku-4-5\)/);
 assert.equal(v.projects[0].saved,'−$2.46'); assert.match(v.projects[1].why,/4 kept by the large-project rule/);
});
test('efficiency ledger shows totals, every lever and projects from receipts',()=>{
 const {efficiencyView}=require('../src/amplifier_fast_decisions/static/app.js');
 const lv=(n,r,c,u,s)=>({label:n,receipts:r,calls_saved:c,usd_saved:u,seconds_saved:s});
 const v=efficiencyView({totals:{receipts:3,calls_saved:1,usd_saved:0.42,seconds_saved:-12,usd_unknown:1,seconds_unknown:0},
  by_lever:{prepared_action:lv('Calls skipped by prepared actions',1,1,0.12,4),cheaper_model:lv('Steps on a cheaper model',2,0,0.3,-16),loop_stop:lv('Loops stopped early',0,0,0,0)},
  by_project:{teaserkit:{cheaper_model:lv('',2,0,0.3,-16)}},excluded_test_receipts:5});
 assert.equal(v.cost,'$0.42'); assert.equal(v.time,'−12 s (slower)'); assert.equal(v.calls,'1');
 assert.equal(v.levers.find(l=>l.key==='loop_stop').active,false);
 assert.equal(v.projects[0].name,'teaserkit'); assert.match(v.note,/5 test\/benchmark receipts excluded/);
 assert.equal(efficiencyView({totals:{receipts:0}}).cost,'—');
});
test('efficiency ledger lists receipts by harness (Claude Code hook)',()=>{
 const {efficiencyView}=require('../src/amplifier_fast_decisions/static/app.js');
 const v=efficiencyView({totals:{receipts:3,calls_saved:2,usd_saved:0.5,seconds_saved:0},
  by_harness:{'Claude Code':{receipts:2,calls_saved:2,usd_saved:0.4,seconds_saved:0},Amplifier:{receipts:1,calls_saved:0,usd_saved:0.1,seconds_saved:0}}});
 assert.equal(v.harnesses[0].name,'Claude Code'); assert.equal(v.harnesses[0].usd,'$0.40'); assert.equal(v.harnesses.length,2);
});
