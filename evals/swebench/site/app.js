'use strict';
const $ = id => document.getElementById(id);
const arms = ['plain-matched', 'jev-prepared', 'laya-prepared', 'jevgrep'];
const labels = {'plain-matched':'Plain Amplifier','jev-prepared':'Jev','laya-prepared':'Laya','jevgrep':'Jevgrep',plain:'Plain Amplifier',jev:'Jev',laya:'Laya',retrieval:'Jevgrep'};
const states = {pending:'Pending',running:'Running',ungraded:'Awaiting grade',resolved:'Resolved',unresolved:'Unresolved',error:'Infrastructure error'};
let data, selectedIssue, selectedExample = 0, metric = 'seconds';
const number = (n, digits = 0) => n == null ? '—' : n.toLocaleString(undefined, {maximumFractionDigits:digits});
const dollars = n => n == null ? 'Unknown' : '$' + n.toLocaleString(undefined,{minimumFractionDigits:2,maximumFractionDigits:n < 1 ? 5 : 2});
const duration = n => n == null ? '—' : n < 120 ? number(n,1)+' s' : number(n/60,1)+' min';
function el(tag, text, className) { const node=document.createElement(tag); if(text != null)node.textContent=text;if(className)node.className=className;return node; }
function issueState(rows) {
  if(!rows || !rows.length)return 'pending';
  for(const state of ['error','running','ungraded','pending','unresolved','resolved'])if(rows.some(r=>r.state===state))return state;
  return 'pending';
}
function renderStudy() {
  const total=data.arms.reduce((sum,a)=>sum+a.planned,0), done=data.arms.reduce((sum,a)=>sum+a.completed,0), graded=data.arms.reduce((sum,a)=>sum+a.graded,0);
  const statusNames={prepared_awaiting_spend_limit:'Awaiting spending limit',running:'Campaign in progress',grading:'Official grading in progress',grading_error:'Grading needs investigation',audit_failed:'Receipt audit needs investigation',needs_reconciliation:'Needs investigation',budget_exhausted:'Spending limit reached',run_limit_reached:'Preflight ready for review',agents_complete_grading_pending:'Grading pending',complete:'Campaign complete'};
  $('study-state').textContent=data.complete?'All runs graded':statusNames[data.status]||'Prepared';
  $('coverage').textContent=`${number(done)} of ${number(total)} agent runs finished. ${number(graded)} officially graded.`;
  $('status-detail').textContent=(data.complete?'Full coverage is available below. One repetition does not establish run-to-run stability.':'The full comparison is incomplete. There is no SWE-bench savings conclusion yet.')+(data.cap!=null?` Estimated-spend limit: ${dollars(data.cap)}.`:'')+(data.setup_cost?` Retained setup costs: ${dollars(data.setup_cost)}.`:'');
  const table=$('arm-table');table.replaceChildren();
  for(const arm of data.arms){const row=el('tr'),name=el('td'),label=el('span',null,'arm-name');label.append(el('i',null,arm.id),el('span',arm.label));name.append(label);row.append(name);
    const cost=arm.completed===0?'—':arm.unknown_cost?`${dollars(arm.known_cost)} known + ${arm.unknown_cost} unknown` : dollars(arm.total_cost);
    for(const value of [`${arm.completed} / ${arm.planned}`,`${arm.graded} / ${arm.planned}`,arm.graded?`${arm.resolved} / ${arm.planned}`:'—',duration(arm.median_seconds),cost])row.append(el('td',value,value==='—'?'null':''));table.append(row);
  }
  $('provenance').textContent=`Dataset revision ${data.dataset_revision || 'unavailable'}. Runtime candidate ${data.candidate || 'unavailable'}. Answering model: ${data.model || 'unavailable'}.`;
  renderIssues();renderExample();renderJudges();renderPairs();
}
function renderIssues(){
  const query=$('issue-search').value.trim().toLowerCase(),filter=$('issue-filter').value;
  const shown=data.issues.filter(issue=>{const rows=Object.values(issue.arms).flat();return issue.id.toLowerCase().includes(query)&&(filter==='all'||filter==='started'&&rows.some(r=>r.state!=='pending')||filter==='resolved'&&rows.some(r=>r.state==='resolved')||filter==='error'&&rows.some(r=>r.state==='error')||filter==='pending'&&rows.every(r=>r.state==='pending'));});
  $('filter-count').textContent=`${number(shown.length)} of ${number(data.issues.length)} issues shown`;
  if(!selectedIssue&&shown.length)selectedIssue=shown[0].id;
  const grid=$('issue-grid');grid.replaceChildren();
  if(!shown.length)grid.append(el('p','No issues match. Try another repository or clear the filter.','empty-filter'));
  for(const issue of shown){const button=el('button',null,'issue-cell');button.type='button';button.setAttribute('aria-label',issue.id+': '+arms.map(arm=>labels[arm]+' '+states[issueState(issue.arms[arm])]).join(', '));button.title=issue.id;button.setAttribute('aria-pressed',String(issue.id===selectedIssue));
    for(const arm of arms){const strip=el('i',null,issueState(issue.arms[arm]));strip.setAttribute('aria-hidden','true');button.append(strip);}button.addEventListener('click',()=>{selectedIssue=issue.id;renderIssues();});grid.append(button);
  }
  if(!selectedIssue&&shown.length)selectedIssue=shown[0].id;
  renderDetail();
}
function renderDetail(){const issue=data.issues.find(i=>i.id===selectedIssue);if(!issue)return;const target=$('issue-detail');target.replaceChildren(el('h3',issue.id),el('p','Recorded runs for the same software issue.'));
  for(const arm of arms){for(const row of issue.arms[arm]||[]){const section=el('div',null,'issue-row'),header=el('header');header.append(el('strong',labels[arm]+(row.rep>1?' · run '+row.rep:'')),el('span',states[row.state],'status-word'));section.append(header);
    if(row.state==='pending')section.append(el('small','Scheduled. No measurements yet.'));else section.append(el('small',`${duration(row.seconds)} wall time · ${dollars(row.cost)} estimated · ${number(row.provider_calls)} model calls`),el('small',`${row.judge_scored} judge scores · ${row.fast_submissions} fast submissions`));target.append(section);
  }}
}
function renderExample(){
  const tabs=$('example-tabs');tabs.replaceChildren();
  data.examples.forEach((example,index)=>{const button=el('button',example.title);button.id='example-tab-'+index;button.type='button';button.setAttribute('role','tab');button.setAttribute('aria-controls','example-panel');button.setAttribute('aria-selected',String(index===selectedExample));button.tabIndex=index===selectedExample?0:-1;button.addEventListener('click',()=>{selectedExample=index;renderExample();$('example-tab-'+index).focus();});button.addEventListener('keydown',event=>{if(['ArrowDown','ArrowRight','ArrowUp','ArrowLeft','Home','End'].includes(event.key)){event.preventDefault();selectedExample=event.key==='Home'?0:event.key==='End'?data.examples.length-1:(index+(['ArrowDown','ArrowRight'].includes(event.key)?1:-1)+data.examples.length)%data.examples.length;renderExample();$('example-tab-'+selectedExample).focus();}});tabs.append(button);});
  const example=data.examples[selectedExample],panel=$('example-panel');panel.setAttribute('aria-labelledby','example-tab-'+selectedExample);panel.replaceChildren(el('h3',example.title),el('p',example.note));
  const toggle=el('div',null,'metric-toggle');for(const [key,label] of [['seconds','Wall time'],['calls','Model calls'],['cost','Total API cost']]){const button=el('button',label);button.type='button';button.setAttribute('aria-pressed',String(key===metric));button.addEventListener('click',()=>{metric=key;renderExample();panel.querySelector('[aria-pressed="true"]').focus();});toggle.append(button);}panel.append(toggle);
  const maximum=Math.max(...example.groups.map(g=>g[metric]??0),1);
  for(const group of example.groups){const row=el('div',null,'bar-row'+(group.arm!=='plain'?' treatment':'')),track=el('div',null,'bar-track'),fill=el('div',null,'bar-fill');fill.style.width=group[metric]==null?'0':(100*group[metric]/maximum)+'%';track.append(fill);const value=metric==='cost'?dollars(group.cost):metric==='seconds'?duration(group.seconds):number(group.calls,1);row.append(el('span',labels[group.arm]),track,el('span',value,'bar-value'));panel.append(row);}
  const footer=el('div',null,'example-foot');footer.append(el('span',example.groups.map(g=>`${labels[g.arm]}: ${g.passed}/${g.runs} task checks passed`).join(' · ')));const link=el('a','Inspect source data');link.href='evidence/'+example.source;link.download='';footer.append(link);panel.append(footer);
}
function renderJudges(){const target=$('judge-comparison');target.replaceChildren();for(const [name,judge] of Object.entries(data.judges)){const row=el('div',null,'judge-line'),time=el('div'),accuracy=el('div');time.append(el('b',number(judge.median_ms,0)+' ms'),el('small','Median classifier latency'));accuracy.append(el('b',judge.correct+'/'+judge.total),el('small','Correct classifications'));row.append(el('strong',labels[name]||name),time,accuracy);target.append(row);}}
function renderPairs(){const panel=$('paired-comparisons');panel.replaceChildren();const available=(data.comparisons||[]).filter(c=>c.time_pairs||c.cost_pairs);if(!available.length)return;panel.append(el('h3','Paired observations against plain Amplifier'),el('p','Ratios below 1 mean less time or cost. These are descriptive observations on completed shared tasks; coverage and quality still matter.'));
for(const c of available){const row=el('div',null,'paired-row');row.append(el('strong',labels[c.arm]),el('span',c.time_ratio==null?'Time unknown':number(c.time_ratio,2)+'× wall time ('+c.time_pairs+' pairs)'),el('span',c.cost_ratio==null?'Cost unknown':number(c.cost_ratio,2)+'× API cost ('+c.cost_pairs+' pairs)'),el('span',c.graded_pairs?(c.resolution_delta>0?'+':'')+c.resolution_delta+' resolved difference / '+c.graded_pairs+' pairs':'Grades pending'));panel.append(row);}}
let loading=false;
async function refresh(){if(loading)return;loading=true;$('refresh-button').disabled=true;try{const response=await fetch('data.json',{cache:'no-store'});if(!response.ok)throw Error('HTTP '+response.status);const next=await response.json();if(next.schema!=='fast-decisions-public-study-v1')throw Error('Unsupported study data');data=next;renderStudy();$('freshness').textContent='Data read '+new Date().toLocaleTimeString([], {hour:'2-digit',minute:'2-digit'});$('freshness').classList.remove('error-note');}catch(error){$('freshness').textContent='Refresh failed. '+(data?'Showing last loaded data.':'Serve this folder over HTTP to load the data.');$('freshness').classList.add('error-note');}finally{loading=false;$('refresh-button').disabled=false;}}
$('issue-search').addEventListener('input',()=>data&&renderIssues());$('issue-filter').addEventListener('change',()=>data&&renderIssues());$('refresh-button').addEventListener('click',refresh);refresh();setInterval(()=>{if(!document.hidden)refresh();},30000);
