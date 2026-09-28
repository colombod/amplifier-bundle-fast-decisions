/* Read-only event projection. No generated events or inferred savings.
   Arrival animations are triggered only by newly received records.
   The pure projections at the top are exported for tests/test_viewer.cjs; everything
   after the `document` guard is the browser render layer. */
(() => {
  'use strict';

  // ---------- pure projections ----------
  const kind = e => e.event.replace('fast_decisions:', '');
  const native = e => e.data.native_event || '';
  const decisionKey = e => e.session_id + ':' + e.decision_id;
  const isScore = e => ['scored', 'shadow_proposed'].includes(kind(e));
  const stamp = e => Date.parse(e.timestamp) || 0;
  const fmt = n => Number.isFinite(n) ? (n >= 1000 ? (n / 1000).toFixed(2) + ' s' : (n > 0 && n < 1 ? n.toFixed(1) : Math.round(n)) + ' ms') : '—';
  const pretty = s => String(s || '').replaceAll('_', ' ');
  const valid = e => e && e.schema_version === '1.0' && typeof e.event_id === 'string' && typeof e.session_id === 'string' && typeof e.event === 'string' && e.event.startsWith('fast_decisions:') && e.data && typeof e.data === 'object' && !Array.isArray(e.data);
  const carriesConfig = e => e.data.phase === 'configuration' || kind(e) === 'turn_start' || e.data.mode === 'advisory' || typeof e.data.session_label === 'string' || typeof e.data.workspace_name === 'string' || typeof e.data.repo === 'string';
  // Display name for a session: an explicit label wins, then the working-directory
  // basename reported by the hook (never a full path), then the short id.
  const repoLabel = config => config.repo ? config.repo + (config.subdir ? '/' + config.subdir : '') : '';
  const sessionName = (config, id) => config.session_label || repoLabel(config) || config.workspace_name || id.slice(0, 8);
  const HARNESS = { claude: 'Claude Code', codex: 'Codex', copilot: 'GitHub Copilot', cursor: 'Cursor', gemini: 'Gemini CLI', grok: 'Grok', opencode: 'OpenCode', amplifier: 'Amplifier', other: 'Other tool' };
  // `engine` is the portable tool's caller label (claude, codex, ...); the loop also
  // records its own engine name there, which is not a harness.
  const harnessOf = config => config.harness || (config.engine && HARNESS[config.engine] ? HARNESS[config.engine] : '');
  function scriptedKeys(events) {
    return new Set(events.filter(e => e.decision_id && (e.synthetic || e.data.synthetic || (isScore(e) && e.data.backend === 'scripted-demo'))).map(decisionKey));
  }
  const synthetic = (e, keys) => !!(e.synthetic || e.data.synthetic || (e.decision_id && !['health', 'observatory'].includes(kind(e)) && keys.has(decisionKey(e))));
  const issue = e => (e.data.phase === 'advisory_result' && e.data.success === false) || ['provider:error', 'provider:retry'].includes(native(e)) || ['error', 'cancelled'].includes(e.data.status) || kind(e) === 'fallback';
  const useful = e => kind(e) !== 'health' || native(e) || e.data.reason_code || ['configuration', 'session_closed', 'advisory_result'].includes(e.data.phase);
  // Operator-set display names (e.g. a Jev-compatible server), learned from
  // each session's configuration event.
  const backendLabels = new Map();
  const noteLabel = e => { if (e && e.data && e.data.phase === 'configuration' && typeof e.data.backend_label === 'string' && e.data.backend_label) backendLabels.set(e.session_id, e.data.backend_label); };
  const labelOf = e => backendLabels.get(e.session_id) || (e.data && typeof e.data.backend_label === 'string' ? e.data.backend_label : '');
  // The once-per-request routing decision, by turn: every model call in the
  // turn follows it, so rows for later calls can show why they ran where.
  const turnJudgments = new Map();
  const noteTurn = e => { if (e && kind(e) === 'difficulty_judged' && e.turn_id) turnJudgments.set(e.turn_id, e); };
  const JUDGE_REASON = { scope_strong: 'Large project · kept on your usual model', user_model_strong: 'Your model pick', judge_cheap: 'Judged easy', judge_strong: 'Judged hard', rules_cheap: 'Short request (built-in rule)', rules_strong: 'Long request (built-in rule)', scope_judge_cheap: 'Large project · judged safe for a faster model', scope_judge_strong: 'Large project · judged to need your usual model', scope_fallback_strong: 'Large project · no judge answer, kept on your usual model' };
  function judgmentView(j, backendLabel) {
    if (!j) return null;
    const d = j.data, pc = d.probabilities && typeof d.probabilities.complex === 'number' ? d.probabilities.complex : null;
    const who = String(d.reason_code || '').startsWith('judge_') ? backendName(d.backend, backendLabel) : String(d.reason_code || '').startsWith('rules_') ? 'Built-in rule' : d.reason_code === 'scope_strong' ? 'Large-project rule' : d.reason_code === 'user_model_strong' ? 'You' : backendName(d.backend, backendLabel);
    return { title: d.choice === 'cheap' ? 'Easy → faster model' : 'Hard → usual model', detail: (JUDGE_REASON[d.reason_code] || pretty(d.reason_code)) + ' · ' + who + (pc !== null ? ' · ' + Math.round(pc * 100) + '% hard' : '') + (d.reason_code === 'scope_strong' && Number.isFinite(d.candidate_count) ? ' · ' + d.candidate_count + ' files' : ''), choice: d.choice, probability: pc === null ? NaN : (d.choice === 'cheap' ? 1 - pc : pc) };
  }
  // Which model a provider call ran on, from its receipts.
  function modelView(group) {
    const end = group.findLast(e => kind(e) === 'slow_end') || group.findLast(e => kind(e) === 'slow_start');
    const mr = group.findLast(e => kind(e) === 'model_routed');
    if (!end && !mr) return null;
    const model = end?.data.model && end.data.model !== 'provider-default' ? end.data.model : (mr?.data.requested_model || null);
    const reason = mr?.data.reason_code || '';
    const faster = reason === 'start_model' || (!!model && !!mr?.data.requested_model && model === mr.data.requested_model && !String(reason).startsWith('escalated'));
    const usual = end?.data.host_model || 'your usual model';
    return { faster, model: faster ? model : usual, switched: String(reason).startsWith('escalated'), reason };
  }
  const backendName = (b, label) => label || ({ 'scripted-demo': 'Scripted scorer', deterministic: 'Scripted scorer', 'ollama-token': 'Local model', ollama: 'Local model', jev: 'Jev', unavailable: 'No scorer' }[b] || b || 'Backend not recorded');
  const money = v => (v < 0 ? '−' : '') + (Math.abs(v) >= 100 ? '$' + Math.round(Math.abs(v)) : '$' + Math.abs(v).toFixed(2));
  const minutes = s => (s >= 3600 ? (s / 3600).toFixed(1) + ' h' : s >= 60 ? Math.round(s / 60) + ' min' : Math.round(s) + ' s');
  const ageOf = t => { const n = Math.max(0, Math.floor((Date.now() - t) / 1000)); return n < 60 ? n + 's ago' : n < 3600 ? Math.floor(n / 60) + 'm ago' : n < 86400 ? Math.floor(n / 3600) + 'h ago' : Math.floor(n / 86400) + 'd ago'; };
  const shortModel = m => String(m || '').replace(/^claude-/, '').replace(/-\d{8}$/, '');
  const hostLabel = r => { const hs = Object.keys(r.cheap_turn_hosts || {}); return hs.length > 1 ? 'each session\'s usual model (' + hs.slice(0, 3).map(shortModel).join(', ') + ')' : hs.length === 1 ? hs[0] : (r.host_model || 'the host model'); };
  function projectRows(r) {
    return (r.by_project || []).slice(0, 6).map(p => ({ name: p.project, turns: p.cheap_turns + ' of ' + (p.cheap_turns + p.strong_turns) + ' on the faster model',
      saved: money(p.saved_usd || 0), why: (p.by_reason && p.by_reason.scope_strong) ? p.by_reason.scope_strong + ' kept by the large-project rule' : '' }));
  }
  const signedMoney = v => (v > 0 ? '' : '') + money(v);
  const signedTime = v => (v < 0 ? '−' + minutes(-v) + ' (slower)' : minutes(v));
  function efficiencyView(r) {
    const t = (r && r.totals) || { receipts: 0 };
    const levers = Object.entries((r && r.by_lever) || {}).map(([k, v]) => ({ key: k, name: v.label || k, receipts: v.receipts,
      calls: v.calls_saved, usd: money(v.usd_saved || 0), secs: signedTime(v.seconds_saved || 0), active: v.receipts > 0 }));
    const projects = Object.entries((r && r.by_project) || {}).map(([name, lv]) => {
      const sum = k => Object.values(lv).reduce((a, v) => a + (v[k] || 0), 0);
      return { name, receipts: sum('receipts'), calls: sum('calls_saved'), usd: money(sum('usd_saved')), secs: signedTime(sum('seconds_saved')) };
    }).sort((a, b) => b.receipts - a.receipts);
    const harnesses = Object.entries((r && r.by_harness) || {}).map(([name, v]) => ({ name, receipts: v.receipts,
      calls: v.calls_saved, usd: money(v.usd_saved || 0), secs: signedTime(v.seconds_saved || 0) }))
      .sort((a, b) => b.receipts - a.receipts);
    return {
      cost: t.receipts ? money(t.usd_saved || 0) : '—', time: t.receipts ? signedTime(t.seconds_saved || 0) : '—',
      calls: t.receipts ? String(t.calls_saved) : '—',
      costDetail: t.receipts ? t.receipts + ' receipts' + (t.usd_unknown ? ' · ' + t.usd_unknown + ' without a cost baseline' : '') : 'No efficiency receipts yet',
      timeDetail: t.receipts && t.seconds_unknown ? t.seconds_unknown + ' without a time baseline' : 'vs the same steps on the default model',
      callsDetail: 'model calls avoided (negative = added)',
      levers, projects, harnesses,
      note: 'Every number is a plain sum of stored receipts (fast_decisions:efficiency); each receipt fixed its baseline and savings when the decision was made. ' +
        ((r && r.excluded_test_receipts) ? r.excluded_test_receipts + ' test/benchmark receipts excluded. ' : '') + 'Recompute with: afast efficiency --json',
    };
  }
  function savingsView(r) {
    const t = r.turns || {}, c = r.cost || {}, tm = r.time || {};
    if (!t.total) return { scope: 'No routed turns recorded yet', cost: '—', costDetail: 'Appears once the orchestrator routes turns.', time: '—', timeDetail: '', turns: '0', turnsDetail: '', note: 'Estimates cover only turns the orchestrator routes to the cheaper model.' };
    const view = {
      scope: 'All recorded sessions · ' + r.files + ' session files',
      cost: (c.saved_usd || 0) < 0 ? money(c.saved_usd) + ' (costs more)' : money(c.saved_usd || 0),
      costDetail: money(c.cheap_turns_actual_usd || 0) + ' spent vs ' + money(c.cheap_turns_on_host_usd || 0) + ' on ' + hostLabel(r),
      time: tm.available ? ((tm.saved_seconds || 0) < 0 ? '−' + minutes(-tm.saved_seconds) + ' (slower)' : minutes(tm.saved_seconds || 0)) : 'Not yet',
      timeDetail: tm.available ? minutes(tm.cheap_turns_model_seconds || 0) + ' on the faster model vs ' + minutes(tm.cheap_turns_on_host_seconds || 0) + ' at your usual model\'s measured speed' + ((tm.saved_seconds || 0) < 0 ? ' — on your requests the "faster" model wrote more slowly' : '') : 'Needs more measured requests on both models',
      turns: t.cheap + ' of ' + t.total,
      projects: projectRows(r),
      turnsDetail: (t.judge_calls ? t.judge_calls + ' judged by the decision-maker; ' : '') + t.strong + ' kept on the host model',
      note: 'Estimates: the same recorded work priced and timed at host-model rates. Host-model turns are unchanged, so they save nothing. Tool time, start-up and decision-maker calls are not counted.',
    };
    const rc = r.recent || {};
    if (rc.turns_since) {
      const reasons = rc.turns_since_by_reason || {}, big = reasons.scope_strong || 0;
      view.recent = (rc.last_cheap_turn_at ? 'Last request on the faster model ' + ageOf(Date.parse(rc.last_cheap_turn_at)) + '. ' : '') + 'Since then ' + rc.turns_since + ' request' + (rc.turns_since === 1 ? '' : 's') + ' stayed on your usual model' + (big ? ' (' + big + ' in large projects, where the size rule always keeps the usual model)' : '') + ', so the totals are not moving.';
      view.note = view.recent + ' ' + view.note;
    }
    if (c.requests_without_cache_data) view.note += ' ' + c.requests_without_cache_data + ' older requests lack cache data, so their dollars are overstated.';
    return view;
  }
  function configFor(events, id) {
    return events.filter(e => e.session_id === id && carriesConfig(e)).reduce((config, e) => ({ ...config, ...e.data }), {});
  }
  const WORKING = new Set(['execution:start', 'provider:request', 'tool:pre', 'tool:post', 'llm:response', 'context:compaction']);
  const LIFE = new Set([...WORKING, 'execution:end', 'session:end', 'provider:error', 'provider:retry']);
  function sessionsFor(events, now) {
    const map = new Map();
    for (const e of events) {
      let s = map.get(e.session_id);
      if (!s) { s = { id: e.session_id, parent: null, events: [], latest: e, config: {} }; map.set(s.id, s); }
      s.events.push(e);
      if (e.parent_session_id) s.parent = e.parent_session_id;
      if (stamp(e) >= stamp(s.latest)) s.latest = e;
      if (carriesConfig(e)) s.config = { ...s.config, ...e.data };
    }
    for (const s of map.values()) {
      const life = s.events.findLast(e => LIFE.has(native(e)) || ['turn_start', 'turn_end'].includes(kind(e)) || e.data.phase === 'session_closed');
      const beat = s.events.findLast(e => ['session_heartbeat', 'configuration'].includes(e.data.phase));
      const closed = life && (life.data.phase === 'session_closed' || native(life) === 'session:end');
      s.reporting = !closed && !!beat && now - stamp(beat) < 45000;
      s.name = sessionName(s.config, s.id);
      if (s.config.mode === 'advisory') { s.state = 'Advisory invocation recorded'; s.stateKind = 'advisory'; }
      else if (closed) { s.state = 'Session closed'; s.stateKind = 'closed'; }
      else if (!s.reporting) { s.state = 'No recent heartbeat'; s.stateKind = 'stale'; }
      else if (!life) { s.state = 'Connected, idle'; s.stateKind = 'idle'; }
      else if (issue(life)) { s.state = 'Provider issue reported'; s.stateKind = 'issue'; }
      else if (native(life) === 'execution:end' || kind(life) === 'turn_end') { s.state = 'Connected, idle'; s.stateKind = 'idle'; }
      else { s.state = 'Working'; s.stateKind = 'working'; }
    }
    return [...map.values()].sort((a, b) => stamp(b.latest) - stamp(a.latest));
  }
  function metrics(events) {
    const scores = events.filter(isScore), fast = new Set(events.filter(e => kind(e) === 'routed' && e.data.route === 'fast').map(decisionKey));
    const executions = events.filter(e => kind(e) === 'tool_end' && (e.data.success === true || e.data.status === 'ok'));
    const nativePosts = events.filter(e => native(e) === 'tool:post');
    const postSessions = new Set(nativePosts.map(e => e.session_id));
    const judged = events.filter(e => kind(e) === 'difficulty_judged');
    const durations = scores.concat(judged.filter(e => String(e.data.reason_code || '').startsWith('judge_'))).map(e => e.data.duration_ms).filter(Number.isFinite).sort((a, b) => a - b);
    const routedCalls = events.filter(e => kind(e) === 'model_routed');
    return {
      judged: judged.length, cheapCalls: routedCalls.filter(e => e.data.reason_code === 'start_model').length,
      hostCalls: routedCalls.filter(e => e.data.reason_code !== 'start_model').length,
      switched: routedCalls.filter(e => String(e.data.reason_code || '').startsWith('escalated')).length,
      scores: scores.length, fast: fast.size, fastExecuted: executions.filter(e => fast.has(decisionKey(e))).length,
      tools: nativePosts.length + executions.filter(e => !postSessions.has(e.session_id)).length,
      bypassed: new Set(events.filter(e => kind(e) === 'routed' && e.data.route === 'fast' && e.data.status === 'submitted_to_upstream').map(decisionKey)).size,
      comparisons: events.filter(e => kind(e) === 'shadow_agreement' && ['match', 'mismatch'].includes(e.data.agreement)),
      failedFast: events.filter(e => kind(e) === 'tool_end' && fast.has(decisionKey(e)) && (e.data.status === 'error' || e.data.success === false)).length,
      issues: events.filter(issue).length, p95: durations.length ? durations[Math.ceil(durations.length * .95) - 1] : NaN,
    };
  }
  const STAGES = new Set(['requested', 'scored', 'shadow_proposed', 'shadow_observed', 'routed', 'fallback', 'tool_start', 'tool_end', 'slow_start', 'slow_end', 'shadow_agreement', 'role_proposed', 'role_agreement']);
  function decisionPath(events, session, selected) {
    const focus = events.find(e => e.event_id === selected && e.decision_id && e.session_id === session)
      || events.findLast(e => e.session_id === session && e.decision_id && kind(e) === 'scored')
      || events.findLast(e => e.session_id === session && e.decision_id && kind(e) === 'requested')
      || events.findLast(e => e.session_id === session && e.decision_id && isScore(e));
    if (!focus) return [];
    return events.filter(e => decisionKey(e) === decisionKey(focus) && STAGES.has(kind(e)))
      .sort((a, b) => stamp(a) - stamp(b) || (a.seq || 0) - (b.seq || 0));
  }
  function describe(e, simulated = false) {
    const k = kind(e), d = e.data, n = native(e);
    let title = pretty(k), detail = d.reason_code ? pretty(d.reason_code) : d.status || 'Recorded metadata', source = 'Runtime record', tone = 'runtime';
    if (n) { title = ({ 'execution:start': 'Turn started', 'execution:end': 'Turn finished', 'session:end': 'Session ended', 'provider:request': 'Provider request', 'provider:retry': 'Provider retry', 'provider:error': 'Provider error', 'tool:pre': 'Tool proposed', 'tool:post': 'Tool result observed', 'context:compaction': 'Context compaction observed', 'llm:response': 'Provider response observed' }[n] || n); detail = [d.tool || d.provider, d.exception_type, d.retry_attempt ? 'Attempt ' + d.retry_attempt : '', d.status_code ? 'HTTP ' + d.status_code : ''].filter(Boolean).join(' · ') || 'Reported by an Amplifier hook'; source = 'Native hook observation'; }
    if (d.phase === 'configuration') { title = 'Bundle mounted'; detail = [d.harness, backendName(d.backend, labelOf(e)), d.mode || 'Mode not recorded', repoLabel(d) || d.workspace_name, d.branch ? '⎇ ' + d.branch : ''].filter(Boolean).join(' · '); source = 'Runtime configuration'; }
    if (d.phase === 'session_closed') { title = 'Telemetry session closed'; detail = 'The bundle observer was unmounted.'; }
    if (k === 'requested') { title = 'Decision requested'; detail = (d.candidate_count ?? '?') + ' prepared candidates'; source = 'Decision service'; tone = 'decision'; }
    if (isScore(e)) { title = k === 'shadow_proposed' ? 'Shadow proposal scored' : 'Decision scored'; detail = d.model || backendName(d.backend, labelOf(e)); source = simulated ? 'Scripted score' : 'Model score'; tone = 'decision'; }
    if (d.mode === 'advisory' && isScore(e)) { title = 'Advisory decision scored'; source = 'Portable Smart Tool'; }
    if (d.phase === 'advisory_result') { title = 'Advisory result returned'; detail = 'Caller owns execution and policy; no bypass claimed.'; source = 'Portable Smart Tool'; }
    if (k === 'routed') { title = d.route === 'fast' ? 'Fast action submitted' : 'Reasoning provider selected'; detail = pretty(d.reason_code); source = 'Routing decision'; tone = 'decision'; }
    if (k === 'tool_start') { title = 'Tool execution started'; detail = d.tool || 'Tool not recorded'; source = 'Measured execution'; }
    if (k === 'tool_end') { title = d.success === false || d.status === 'error' ? 'Tool execution failed' : 'Tool execution ' + (d.status === 'ok' ? 'completed' : (d.status || 'ended')); detail = d.tool || 'Tool not recorded'; source = 'Measured execution'; }
    if (k === 'slow_start' || k === 'slow_end') { title = k === 'slow_start' ? 'Model invocation started' : 'Model invocation ended'; detail = d.provider || d.model || 'Provider not recorded'; source = 'Measured invocation'; }
    if (k === 'shadow_observed') { title = 'Tool choice observed'; detail = d.tool || 'Tool not recorded'; source = 'Shadow observation'; }
    if (k === 'shadow_agreement') { title = 'Shadow comparison: ' + pretty(d.agreement); detail = 'Compared with the observed tool choice; no execution changed.'; source = 'Shadow comparison'; }
    if (k === 'role_proposed' || k === 'role_agreement') { title = 'Model-role ' + (k === 'role_proposed' ? 'suggestion' : 'comparison'); detail = 'Shadow only; provider selection is unchanged.'; source = 'Shadow comparison'; }
    if (d.reason_code === 'no_eligible_candidates') { title = 'No prepared action available'; detail = 'The request did not produce an eligible candidate.'; }
    if (k === 'difficulty_judged') { const jv = judgmentView(e, labelOf(e)); title = 'Request routed: ' + jv.title; detail = jv.detail; source = 'Difficulty router'; }
    if (d.reason_code === 'judge_disabled') { title = 'Routed to model (routing-only)'; detail = 'No judge configured; effort and model routing still apply.'; }
    if (d.reason_code === 'provider_not_matched') { title = 'Model routing skipped'; detail = 'This provider does not match model_routing.provider_match; its own model is used.'; }
    if (k === 'turn_start') { title = 'Hybrid turn started'; detail = backendName(d.backend, labelOf(e)) + ' · ' + d.mode; }
    if (k === 'turn_end') { title = 'Hybrid turn finished'; detail = (d.fast_total ?? 0) + ' fast submissions · ' + (d.slow_total ?? 0) + ' measured provider calls'; }
    if (k === 'observatory') { title = 'Viewer ' + (d.action || 'event'); detail = pretty(d.reason) || 'Viewer lifecycle metadata'; }
    if (issue(e)) tone = 'error';
    if (simulated) { source = 'Synthetic / scripted'; tone = 'synthetic'; }
    return { title, detail, source, tone };
  }
  // One decision group -> the ledger row: what was proposed, what happened, and a verdict.
  // Every verdict is a label with a tone; the tone never carries meaning alone.
  function summarize(group, simulated, opts = {}) {
    const by = k => group.find(e => kind(e) === k), last = k => group.findLast(e => kind(e) === k);
    const nat = n => group.findLast(e => native(e) === n);
    const request = by('requested'), score = last('scored') || last('shadow_proposed');
    const routed = last('routed'), fallback = last('fallback'), agreement = last('shadow_agreement');
    const toolEnd = last('tool_end'), slowEnd = last('slow_end'), observed = last('shadow_observed') || nat('tool:pre');
    const roleProposed = last('role_proposed'), roleAgreement = last('role_agreement');
    const advisory = group.some(e => e.data.mode === 'advisory' || e.data.phase === 'advisory_result');
    const judgment = group.find(e => kind(e) === 'difficulty_judged') || turnJudgments.get(group.find(e => e.turn_id)?.turn_id);
    const jv = judgmentView(judgment, judgment ? labelOf(judgment) : '');
    const mv = modelView(group);
    const names = new Map((request?.data.candidates || []).map(c => [c.id, c.label]));
    names.set('reason', 'Defer to reasoning model');
    const label = id => names.get(id) || id; // a raw candidate id stays verbatim (rendered monospace)
    const choice = score?.data.choice;
    const probability = score ? (Number.isFinite(score.data.selected_probability) ? score.data.selected_probability : score.data.probabilities?.[choice]) : NaN;
    const submitted = routed?.data.route === 'fast' && routed.data.status === 'submitted_to_upstream';
    const toolOK = !!toolEnd && (toolEnd.data.success === true || toolEnd.data.status === 'ok') && !['error', 'cancelled'].includes(toolEnd.data.status) && toolEnd.data.success !== false;
    const providerOK = slowEnd?.data.status === 'ok';
    const terminal = !!(toolEnd || slowEnd || agreement || fallback || roleAgreement || nat('tool:post') || group.some(e => e.data.phase === 'advisory_result') || (routed && routed.data.route !== 'fast' && !opts.live));
    const inFlight = !!opts.live && !terminal && (opts.now - stamp(group.at(-1))) < 90000;
    // proposed
    let proposed = { title: 'No FD judgment recorded', detail: 'Tool activity only' };
    if (roleProposed && !score) proposed = { title: roleProposed.data.proposed_model_role ? 'Role: ' + roleProposed.data.proposed_model_role : 'Role router abstained', detail: pretty(roleProposed.data.reason_code) };
    else if (score) proposed = { title: choice ? label(choice) : 'No choice recorded', id: !!choice && !names.has(choice), detail: (score.data.model || backendName(score.data.backend, labelOf(score))) + (request ? ' · ' + (request.data.candidate_count ?? '?') + ' candidates' : ''), probability };
    else if (fallback) proposed = { title: fallback.data.reason_code === 'no_eligible_candidates' ? 'No prepared action' : 'Fallback', detail: pretty(fallback.data.reason_code) };
    else if (request) proposed = { title: 'Awaiting score', detail: (request.data.candidate_count ?? '?') + ' candidates' };
    if (!score && !roleProposed && jv) proposed = { title: jv.title, detail: jv.detail, probability: jv.probability };
    // happened
    let happened = { title: '—', detail: '' };
    if (routed?.data.route === 'fast') happened = { title: 'Fast action → ' + (routed.data.destination || routed.data.selected_candidate || 'tool'), detail: toolEnd ? (!toolOK ? 'execution ' + (toolEnd.data.status || 'outcome unknown') : 'executed' + (Number.isFinite(toolEnd.data.duration_ms) ? ' in ' + fmt(toolEnd.data.duration_ms) : '')) : routed.data.status === 'submitted_to_upstream' ? 'submitted to upstream · no execution recorded' : pretty(routed.data.status) };
    else if (routed && mv) happened = { title: (mv.faster ? 'Faster model · ' : 'Usual model · ') + mv.model, detail: (providerOK && Number.isFinite(slowEnd.data.duration_ms) ? 'answered in ' + fmt(slowEnd.data.duration_ms) : slowEnd ? 'provider ' + (slowEnd.data.status || 'outcome unknown') : inFlight ? 'running…' : 'no completion recorded') + (mv.switched ? ' · switched mid-request' : '') };
    else if (routed) happened = { title: 'Reasoning provider' + (slowEnd?.data.provider || routed.data.destination ? ' · ' + (slowEnd?.data.provider || routed.data.destination) : ''), detail: (providerOK && Number.isFinite(slowEnd.data.duration_ms) ? 'answered in ' + fmt(slowEnd.data.duration_ms) + ' · ' : '') + pretty(routed.data.reason_code) };
    else if (advisory) happened = { title: 'Suggestion returned to caller', detail: 'no execution or bypass claimed' };
    else if (observed) happened = { title: 'LLM called ' + (observed.data.tool || 'a tool'), detail: nat('tool:post') ? 'result observed' : inFlight ? 'running…' : 'no result observed' };
    else if (roleAgreement) happened = { title: 'Delegate used role ' + (roleAgreement.data.actual_model_role || 'default'), detail: 'read from the tool result' };
    else if (fallback) happened = { title: 'Deferred to the provider', detail: fallback.data.exception_type || '' };
    else if (score) happened = { title: inFlight ? 'Waiting for the LLM' : 'Outcome not joined', detail: '' };
    // verdict
    let verdict;
    if (simulated) verdict = { label: 'Scripted', tone: 'warn', note: 'Scripted evidence · excluded from counts' };
    else if (advisory) verdict = { label: 'Advisory', tone: 'adv', note: 'Advisory only · no execution or savings claimed' };
    else if (routed?.data.route === 'fast') {
      const bypassNote = submitted ? 'Generative call bypassed' : 'Bypass not confirmed';
      verdict = toolEnd
        ? toolOK ? { label: 'Fast · executed', tone: 'ok', note: bypassNote + ' · tool executed · task quality unverified' }
          : { label: 'Fast · ' + (toolEnd.data.status === 'cancelled' ? 'cancelled' : toolEnd.data.success === false || toolEnd.data.status === 'error' ? 'failed' : 'outcome unknown'), tone: 'bad', note: bypassNote + ' · successful execution not recorded' }
        : { label: submitted ? 'Fast · submitted' : 'Fast · selected', tone: submitted ? 'ok' : 'info', note: bypassNote + ' · no completed tool recorded' };
    }
    else if (routed && mv) verdict = mv.switched ? { label: 'Switched up', tone: 'warn', note: 'Moved to your usual model mid-request (' + pretty(mv.reason) + ')' } : mv.faster ? { label: 'Faster model', tone: 'ok', note: (jv ? jv.detail : 'Routed to the faster model') + (providerOK ? ' · provider answered' : '') } : { label: 'Usual model', tone: 'info', note: (jv ? jv.detail : 'Standard setup') + (providerOK ? ' · provider answered' : '') };
    else if (routed) verdict = { label: 'Reasoning model', tone: 'info', note: pretty(routed.data.reason_code) + (providerOK ? ' · provider answered' : slowEnd ? ' · provider ' + (slowEnd.data.status || 'outcome unknown') : ' · no provider completion recorded') };
    else if (fallback && fallback.data.reason_code === 'no_eligible_candidates') verdict = { label: 'No candidate', tone: 'muted', note: 'No eligible prepared action · provider path unchanged' };
    else if (fallback) verdict = { label: 'Fallback', tone: fallback.data.exception_type ? 'bad' : 'warn', note: pretty(fallback.data.reason_code) + ' · deferred to the provider' };
    else if (agreement) verdict = ({ match: { label: 'Match', tone: 'ok' }, mismatch: { label: 'Mismatch', tone: 'warn' }, abstained: { label: 'Abstained', tone: 'info' } }[agreement.data.agreement] || { label: pretty(agreement.data.agreement), tone: 'muted' });
    else if (roleAgreement) verdict = roleAgreement.data.agreement === 'match' ? { label: 'Role match', tone: 'ok' } : roleAgreement.data.agreement === 'mismatch' ? { label: 'Role mismatch', tone: 'warn' } : { label: 'Role ' + pretty(roleAgreement.data.agreement), tone: 'muted' };
    else if (score && choice === 'reason') verdict = { label: 'Proposed: defer', tone: 'info' };
    else if (score || roleProposed) verdict = inFlight ? { label: 'In flight', tone: 'live' } : { label: 'Unjoined', tone: 'muted', note: 'No matching outcome was recorded' };
    else verdict = inFlight ? { label: 'In flight', tone: 'live' } : { label: 'Observed only', tone: 'muted', note: 'The monitoring hook recorded this call; no Fast Decisions evaluation is linked to it' };
    if (!verdict.note) verdict.note = group.some(e => kind(e).startsWith('shadow_')) ? 'Shadow comparison · execution unchanged' : 'Recorded runtime path · no fast bypass claimed';
    const mode = simulated ? 'Scripted' : advisory ? 'Advisory' : group.some(e => kind(e).startsWith('shadow_') || kind(e).startsWith('role_')) ? 'Shadow' : routed || request ? 'Active' : group.some(e => native(e)) ? 'Native' : 'Runtime';
    // Scoring column: an action score's latency, else the per-request routing
    // decision -- who decided, how fast, and with what confidence.
    let scoring = { value: Number.isFinite(score?.data.duration_ms) ? fmt(score.data.duration_ms) : '—', label: score ? 'scoring' : 'no score' };
    if (!score && judgment) {
      const jd = judgment.data, rc = String(jd.reason_code || ''), pc = jd.probabilities && typeof jd.probabilities.complex === 'number' ? jd.probabilities.complex : null;
      if (rc.startsWith('judge_')) scoring = { value: Number.isFinite(jd.duration_ms) ? fmt(jd.duration_ms) : '—', label: backendName(jd.backend, labelOf(judgment)) + (pc !== null ? ' · ' + Math.round(pc * 100) + '% hard' : '') };
      else if (rc === 'scope_strong') scoring = { value: 'rule', label: 'large project · no judge call' };
      else if (rc === 'user_model_strong') scoring = { value: 'you', label: 'your model pick' };
      else if (rc.startsWith('rules_')) scoring = { value: 'rule', label: 'length rule' + (jd.backend && jd.backend !== 'unavailable' ? ' (judge unavailable)' : '') };
    }
    return { proposed, happened, verdict, inFlight, mode, score, request, latency: score?.data.duration_ms, scoring };
  }
  // Circuit edges require route/invocation evidence. A score, shadow agreement,
  // or advisory result alone never lights an execution path.
  function circuitFor(group) {
    const last = k => group.findLast(e => kind(e) === k);
    const route = last('routed'), score = last('scored') || last('shadow_proposed') || last('difficulty_judged') || turnJudgments.get(group.find(e => e.turn_id)?.turn_id);
    const advisory = group.some(e => e.data.mode === 'advisory' || e.data.phase === 'advisory_result');
    const shadow = group.some(e => kind(e).startsWith('shadow_') || kind(e).startsWith('role_'));
    const provider = last('slow_end') || last('slow_start');
    const fast = !advisory && !shadow && route?.data.route === 'fast' ? route : null;
    const slow = provider || (!advisory && !shadow && route?.data.route === 'slow' ? route : null);
    return { request: last('requested'), score, fast, slow,
      host: group.findLast(e => ['tool_start', 'tool_end'].includes(kind(e)) || ['tool:pre', 'tool:post'].includes(native(e))),
      branch: fast ? 'fast' : slow ? 'slow' : 'unknown',
      receipt: last('tool_end') || last('slow_end') || last('shadow_agreement') || group.at(-1) };
  }
  if (typeof module !== 'undefined') module.exports = { kind, valid, scriptedKeys, synthetic, sessionsFor, metrics, describe, decisionPath, summarize, sessionName, circuitFor, savingsView, harnessOf, efficiencyView };
  if (typeof document === 'undefined') return;

  // ---------- browser render layer ----------
  const $ = id => document.getElementById(id), put = (id, text) => { $(id).textContent = text; };
  const make = (tag, cls, text) => { const node = document.createElement(tag); if (cls) node.className = cls; if (text !== undefined) node.textContent = text; return node; };
  let events = [], seen = new Set(), cursor = 0, epoch = null, session = '', selected = null, category = 'live', following = true, source = 'live', lastPoll = 0, connectionError = '', retained = 0, invalidLines = 0, windowGap = false, hasMore = false;
  let frozenIds = null, expanded = new Set(), token = '', pollTimer = null, pollGeneration = 0, arrivals = new Set(), renderedAt = 0;
  let circuitFocus = null;
  let historyReady = false;
  let lastStudyPoll = 0;
  // Older Ledger/Circuit URLs now open the same dashboard. Collapse only the
  // diagram; the event stream, selection and filters always remain shared.
  try { $('decisionCircuit').open = sessionStorage.getItem('afast-path-collapsed') !== 'true'; } catch (_) { }
  $('decisionCircuit').ontoggle = () => {
    try { sessionStorage.setItem('afast-path-collapsed', String(!$('decisionCircuit').open)); } catch (_) { }
  };
  const tokenKey = 'afast-token:' + location.host;
  try { token = new URLSearchParams(location.hash.slice(1)).get('token') || sessionStorage.getItem(tokenKey) || ''; if (token) sessionStorage.setItem(tokenKey, token); } catch (_) { }
  if (location.hash.startsWith('#token=')) history.replaceState(null, '', location.pathname + location.search);
  const age = t => { const n = Math.max(0, Math.floor((Date.now() - t) / 1000)); return n < 5 ? 'just now' : n < 60 ? n + 's ago' : n < 3600 ? Math.floor(n / 60) + 'm ago' : n < 86400 ? Math.floor(n / 3600) + 'h ago' : Math.floor(n / 86400) + 'd ago'; };
  const clock = e => new Date(e.timestamp).toLocaleTimeString([], { hour12: false, hour: '2-digit', minute: '2-digit', second: '2-digit' });
  const shortId = id => id.length > 12 ? id.slice(0, 8) : id;
  const toneClass = t => ({ ok: 'ok', info: 'info', warn: 'warn', bad: 'bad', adv: 'adv', live: 'live' }[t] || '');

  function ingest(batch, force = false) {
    arrivals = new Set(source === 'live' && historyReady && following ? batch.filter(e => !seen.has(e.event_id)).map(e => e.event_id) : []);
    for (const e of batch) if (valid(e) && !seen.has(e.event_id)) { seen.add(e.event_id); events.push(e); noteLabel(e); noteTurn(e); }
    events.sort((a, b) => stamp(a) - stamp(b) || (a.session_id === b.session_id ? (a.seq || 0) - (b.seq || 0) : 0));
    if (events.length > 20000) { events = events.slice(-20000); seen = new Set(events.map(e => e.event_id)); }
    if (force || batch.length || source !== 'live' || Date.now() - renderedAt > 5000) render();
    arrivals.clear();
  }
  function currentWindow() { const limit = $('timeWindow').value; return events.filter(e => (limit === 'all' || Date.now() - stamp(e) <= Number(limit)) && (!frozenIds || frozenIds.has(e.event_id))); }
  function scopeData(base) {
    const children = $('includeChildren').checked;
    const parents = new Map(events.filter(e => e.parent_session_id).map(e => [e.session_id, e.parent_session_id]));
    const descendant = id => { const visited = new Set(); while (parents.has(id) && !visited.has(id)) { visited.add(id); id = parents.get(id); if (id === session) return true; } return false; };
    return base.filter(e => session ? (e.session_id === session || (children && descendant(e.session_id))) : (children || !parents.has(e.session_id)));
  }
  function selectSession(id) { session = id; selected = null; render(); }

  function renderSessions(base, keys) {
    const sessions = sessionsFor(base, Date.now()), map = new Map(sessions.map(s => [s.id, s]));
    const roots = sessions.filter(s => !s.parent), orphans = sessions.filter(s => s.parent && !map.has(s.parent));
    for (const child of orphans) if (!roots.some(s => s.id === child.parent)) roots.push({ id: child.parent, parent: null, events: [], latest: child.latest, config: {}, name: child.parent.slice(0, 8), state: 'Parent not observed', stateKind: 'stale', reporting: false });
    const search = $('sessionSearch').value.toLowerCase();
    put('sessionCount', roots.length); put('allSessionCount', roots.length);
    $('allSessions').classList.toggle('selected', !session); $('allSessions').setAttribute('aria-pressed', String(!session));
    const target = $('sessions'); target.replaceChildren();
    function row(s, child = false) {
      const button = make('button', 'session-row' + (s.id === session ? ' selected' : '') + (child ? ' child' : ''));
      button.setAttribute('aria-pressed', String(s.id === session)); button.title = s.id; button.dataset.sessionId = s.id;
      const named = !!(s.config.session_label || s.config.repo || s.config.workspace_name);
      const top = make('span', 'session-top');
      top.append(make('span', 'session-name' + (named ? '' : ' unnamed'), s.name), make('span', 'session-age', age(stamp(s.latest))));
      const meta = make('span', 'session-meta');
      if (named) meta.append(make('span', 'session-id', shortId(s.id)));
      meta.append(make('span', 'session-config', s.config.mode ? backendName(s.config.backend, s.config.backend_label) + ' · ' + s.config.mode : 'Configuration not recorded'));
      const context = [harnessOf(s.config), s.config.branch ? '⎇ ' + s.config.branch : ''].filter(Boolean).join(' · ');
      button.append(top);
      if (context) button.append(make('span', 'session-context', context));
      button.append(meta, make('span', 'session-state ' + (s.stateKind || ''), s.state));
      button.title = [sessionName(s.config, s.id), harnessOf(s.config), s.config.branch, s.id].filter(Boolean).join(' · ');
      button.onclick = () => selectSession(s.id); return button;
    }
    function group(s, level = 0, visited = new Set()) {
      if (visited.has(s.id)) return null; visited = new Set([...visited, s.id]);
      const kids = sessions.filter(x => x.parent === s.id);
      const matches = [s, ...kids].some(x => (x.id + ' ' + (x.name || '') + ' ' + (x.config.session_label || '') + ' ' + (x.config.workspace_name || '')).toLowerCase().includes(search));
      if (!matches) return null;
      const wrap = make('div', 'session-group'); wrap.append(row(s, level > 0));
      if (kids.length) {
        const toggle = make('button', 'children-toggle', (expanded.has(s.id) ? 'Hide ' : 'Show ') + kids.length + ' child session' + (kids.length === 1 ? '' : 's'));
        toggle.dataset.childrenId = s.id; toggle.setAttribute('aria-expanded', String(expanded.has(s.id)));
        toggle.onclick = () => { expanded.has(s.id) ? expanded.delete(s.id) : expanded.add(s.id); render(); };
        wrap.append(toggle);
        if (expanded.has(s.id)) for (const child of kids) { const g = group(child, level + 1, visited); if (g) wrap.append(g); }
      }
      return wrap;
    }
    for (const s of roots) { const g = group(s); if (g) target.append(g); }
    if (!roots.length) target.append(make('p', 'session-config', 'No sessions in this window.'));
    return sessions;
  }

  function renderDetails(e, keys) {
    $('detailEmpty').hidden = !!e; $('detailContent').hidden = !e;
    if (!e) { put('evidenceKind', 'No selection'); $('evidenceKind').className = 'tag'; return; }
    const simulated = synthetic(e, keys), info = describe(e, simulated), d = e.data;
    put('evidenceKind', info.source); $('evidenceKind').className = 'tag ' + ({ decision: 'ok', error: 'bad', synthetic: 'warn' }[info.tone] || '');
    put('detailTitle', info.title); put('detailDescription', info.detail);
    const cfg = configFor(events, e.session_id), related = e.decision_id ? events.filter(x => decisionKey(x) === decisionKey(e)) : [e];
    const score = related.findLast(isScore), request = related.find(x => kind(x) === 'requested');
    const facts = [
      ['Session', sessionName(cfg, e.session_id), false], ['Session id', e.session_id, true],
      ...(e.parent_session_id ? [['Parent', e.parent_session_id, true]] : []),
      ...(cfg.workspace_name ? [['Directory', cfg.workspace_name, false]] : []),
      ['Recorded', new Date(e.timestamp).toLocaleString(), false], ['Event', e.event, true], ['Source', info.source, false],
      ...(d.tool ? [['Tool', d.tool, true]] : []), ...(d.choice ? [['Choice', d.choice, true]] : []), ...(d.tool_call_id ? [['Tool call', d.tool_call_id, true]] : []),
      ...(e.decision_id ? [['Decision', e.decision_id, true]] : []),
      ...(Number.isFinite(d.duration_ms) ? [['Measured duration', fmt(d.duration_ms) + (d.latency_kind ? ' · ' + pretty(d.latency_kind) : ''), false]] : []),
      ...(d.reason_code ? [['Reason code', pretty(d.reason_code), false]] : []),
      ...(d.agreement ? [['Agreement', pretty(d.agreement) + (d.proposed_candidate ? ' · proposed ' + d.proposed_candidate : '') + (d.actual_tool ? ' · actual ' + d.actual_tool : ''), false]] : []),
      ...(d.domain ? [['Domain', d.domain, false]] : []),
    ];
    $('eventFacts').replaceChildren();
    for (const [label, value, mono] of facts) { const row = make('div'); row.append(make('dt', '', label), make('dd', mono ? 'mono' : '', value)); $('eventFacts').append(row); }
    put('sessionConfig', cfg.mode ? backendName(cfg.backend, cfg.backend_label) + ' in ' + cfg.mode + ' mode. ' + (cfg.backend === 'scripted-demo' ? 'Scores are scripted; no model is connected to this scorer.' : cfg.mode === 'shadow' ? 'Proposals do not change execution.' : cfg.mode === 'advisory' ? 'Portable suggestion only; caller owns execution.' : 'Fast selections still pass native approval.') : 'No configuration event is available for this session.');
    $('distribution').hidden = !score; $('probabilities').replaceChildren();
    if (score) {
      put('scoreMeaning', synthetic(score, keys) ? 'Scripted values. These did not come from a model.' : score.data.probability_kind === 'token_mass_with_abstention_residual' ? 'Uncalibrated token scores from ' + (score.data.model || backendName(score.data.backend, labelOf(score))) + '. They do not measure correctness.' : 'Backend-reported scores; not independent evidence of correctness.');
      const names = new Map((request?.data.candidates || []).map(c => [c.id, c.label])); names.set('reason', 'Use reasoning model');
      for (const [id, p] of Object.entries(score.data.probabilities || {}).filter(([, p]) => Number.isFinite(p)).sort((a, b) => b[1] - a[1])) {
        const row = make('div', 'prob-row' + (id === score.data.choice ? ' chosen' : '') + (id === 'reason' ? ' reason' : '')), caption = make('div', 'prob-label'), track = make('div', 'prob-track'), fill = make('div', 'prob-fill');
        caption.append(make(id === score.data.choice ? 'b' : 'span', '', (names.get(id) || id) + (id === score.data.choice ? ' · chosen' : '')), make('span', '', (p * 100).toFixed(1) + '%'));
        fill.style.width = Math.max(0, Math.min(100, p * 100)) + '%'; track.append(fill); row.append(caption, track); $('probabilities').append(row);
      }
    }
    put('eventJson', JSON.stringify(e, null, 2));
  }

  // Group events into decision flows. A native tool:pre/tool:post pair joins the decision
  // whose shadow_observed / tool_start recorded the same tool_call_id in the same session;
  // otherwise it forms its own hook-observation flow.
  function flowGroups(scoped) {
    const callToDecision = new Map();
    for (const e of scoped) if (e.decision_id && e.data.tool_call_id) callToDecision.set(e.session_id + ':' + e.data.tool_call_id, decisionKey(e));
    const groups = new Map();
    for (const e of scoped) {
      const nativeCall = !e.decision_id && native(e) && e.data.tool_call_id;
      if (!nativeCall && (!e.decision_id || (['health', 'observatory'].includes(kind(e)) && e.data.phase !== 'advisory_result'))) continue;
      const key = nativeCall ? (callToDecision.get(e.session_id + ':' + e.data.tool_call_id) || e.session_id + ':native:' + e.data.tool_call_id) : decisionKey(e);
      if (!groups.has(key)) groups.set(key, []);
      groups.get(key).push(e);
    }
    for (const g of groups.values()) g.sort((a, b) => stamp(a) - stamp(b) || (a.seq || 0) - (b.seq || 0));
    return groups;
  }
  const isStage = e => STAGES.has(kind(e)) || e.data.phase === 'advisory_result' || (native(e) && e.data.tool_call_id);

  function renderCircuit(rows) {
    const focused = rows.find(row => row.group.some(e => e.event_id === selected));
    const decisions = rows.filter(row => row.group.some(e => e.decision_id));
    const latestRow = (decisions.length ? decisions : rows).reduce((latest, row) => !latest || stamp(row.group.at(-1)) > stamp(latest.group.at(-1)) ? row : latest, null);
    const row = focused || latestRow;
    const group = row?.group || [], s = row?.summary, path = circuitFor(group);
    $('circuitLatest').hidden = !focused;
    const latest = group.at(-1);
    circuitFocus = path.score || path.receipt || null;
    put('circuitContext', latest ? sessionName(configFor(events, latest.session_id), latest.session_id) + ' · ' + (focused ? 'Selected decision' : 'Latest decision') + ' · ' + clock(latest) : 'Start a turn or open a saved trace to follow a decision.');
    put('circuitMode', s?.mode || 'No records');
    $('circuitMode').className = 'tag ' + (s?.mode === 'Scripted' ? 'warn' : s?.mode === 'Advisory' ? 'adv' : '');
    $('circuitMap').dataset.branch = path.branch;
    const newStage = kinds => source === 'live' && following && group.some(e => arrivals.has(e.event_id) && kinds.includes(kind(e)));
    $('circuitMap').classList.toggle('fresh-score', newStage(['scored', 'shadow_proposed']));
    $('circuitMap').classList.toggle('fresh-fast', !!path.fast && newStage(['routed', 'tool_start', 'tool_end']));
    $('circuitMap').classList.toggle('fresh-slow', !!path.slow && newStage(['routed', 'slow_start', 'slow_end']));
    const bind = (id, event) => {
      const node = $(id); node.disabled = !event;
      node.classList.toggle('arriving', !!event && arrivals.has(event.event_id));
      if (event) node.dataset.circuitEventId = event.event_id; else delete node.dataset.circuitEventId;
      node.setAttribute('aria-pressed', String(!!event && selected === event.event_id));
      node.onclick = event ? () => { selected = event.event_id; render(); } : null;
    };
    bind('circuitState', path.request); bind('circuitJudge', path.score);
    bind('circuitFast', path.fast); bind('circuitSlow', path.slow); bind('circuitReceipt', path.receipt);
    bind('circuitHost', path.host);
    put('circuitHostNote', path.host ? describe(path.host).title : 'Permissions stay upstream');
    put('circuitStateNote', path.request ? (path.request.data.candidate_count ?? '?') + ' prepared candidates' : 'No request recorded');
    put('circuitJudgeNote', path.score ? (kind(path.score) === 'difficulty_judged' ? judgmentView(path.score, labelOf(path.score)).title : (path.score.data.model || backendName(path.score.data.backend, labelOf(path.score)))) : 'No score recorded');
    put('circuitFastNote', path.fast ? (path.fast.data.status === 'submitted_to_upstream' ? 'Submitted to host' : 'Selected; not confirmed') : 'No fast route recorded');
    put('circuitSlowNote', path.slow ? ((path.slow.data.model === 'provider-default' ? (path.slow.data.host_model || 'Your usual model') : path.slow.data.model) || path.slow.data.provider || (kind(path.slow) === 'routed' ? 'Selected; not yet invoked' : 'Invocation recorded')) : 'No invocation recorded');
    put('circuitProposed', s?.proposed.title || 'No proposal recorded');
    put('circuitHappened', s?.happened.title === '—' ? 'No outcome recorded' : s?.happened.title || 'No outcome recorded');
    put('circuitOutcomeNote', s?.happened.detail || '');
    put('circuitReceipt', s?.verdict.label || 'Awaiting evidence');
    $('circuitReceipt').className = 'receipt-button ' + (s ? toneClass(s.verdict.tone) : '');
    put('circuitEvidence', s ? s.verdict.note + '. Select a node or receipt to inspect recorded evidence.' : 'Recorded routes light up as events arrive. Select a node to inspect its evidence.');
  }

  function renderFlows(scoped, keys) {
    $('liveFlows').hidden = category !== 'live';
    const now = Date.now();
    const groups = flowGroups(scoped);
    const roots = sessionsFor(scoped, now);
    $('runtimeStrip').replaceChildren();
    for (const item of roots.filter(s => s.stateKind !== 'closed' && s.stateKind !== 'advisory' && (s.reporting || (now - stamp(s.latest) < 120000 && s.events.some(e => native(e))))).slice(0, 8)) {
      const observed = item.events.findLast(e => native(e)), row = make('button', 'runtime-pulse ' + (item.stateKind || '') + (arrivals.has(observed?.event_id) ? ' arriving' : ''));
      row.dataset.runtimeSessionId = item.id;
      row.append(make('span', 'live-dot'), make('strong', '', item.name), make('span', '', (item.parent ? 'child · ' : '') + (item.reporting ? item.state.toLowerCase() : 'presence unknown') + (observed ? ' · ' + describe(observed).title.toLowerCase() + ' ' + age(stamp(observed)) : '')));
      row.onclick = () => selectSession(item.id); $('runtimeStrip').append(row);
    }
    const live = source === 'live';
    const rows = [...groups.entries()].map(([key, group]) => ({ key, group, summary: summarize(group, synthetic(group.at(-1), keys), { live, now }) }));
    rows.sort((a, b) => (b.summary.inFlight - a.summary.inFlight) || stamp(b.group.at(-1)) - stamp(a.group.at(-1)));
    renderCircuit(rows);
    if (category !== 'live') return;
    const shown = rows.slice(0, 40);
    put('flowStatus', source !== 'live' ? 'Saved records · ' + rows.length + ' decision flows' : !following ? 'Paused · ' + rows.length + ' decision flows' : rows.length ? (rows.length > shown.length ? 'Latest ' + shown.length + ' of ' + rows.length + ' decision flows' : rows.length + ' decision flow' + (rows.length === 1 ? '' : 's') + ' · following live') : 'Waiting for decisions');
    $('flowCards').replaceChildren(); $('flowEmpty').hidden = rows.length > 0;
    for (const { key, group, summary } of shown) {
      const first = group[0], latest = group.at(-1), cfg = configFor(events, latest.session_id);
      const simulated = synthetic(latest, keys), s = summary;
      const card = make('article', 'flow-card' + (s.inFlight ? ' in-flight' : '') + (s.mode === 'Native' ? ' native' : '') + (group.some(e => arrivals.has(e.event_id)) ? ' arriving' : '') + (group.some(e => e.event_id === selected) ? ' selected-card' : ''));
      card.dataset.decisionKey = key;
      // summary row
      const line = make('div', 'flow-summary');
      const when = make('span', 'flow-when', clock(first)); when.append(make('small', '', age(stamp(latest))));
      const sessionWrap = make('span', 'flow-session-wrap');
      const sessionButton = make('button', 'flow-session clamp2', sessionName(cfg, latest.session_id)); sessionButton.title = latest.session_id;
      sessionButton.onclick = () => selectSession(latest.session_id);
      const tagline = make('span', 'tagline');
      tagline.append(make('span', 'tag ' + ({ Scripted: 'warn', Advisory: 'adv', Active: 'ok', Shadow: '', Native: '', Runtime: '' }[s.mode]), s.mode));
      if (latest.parent_session_id) { const parentName = sessionName(configFor(events, latest.parent_session_id), latest.parent_session_id); const parent = make('span', 'flow-parent', 'child of ' + parentName); parent.title = 'Child of ' + parentName + ' (' + latest.parent_session_id + ')'; tagline.append(parent); }
      sessionWrap.append(sessionButton, tagline);
      const cell = (heading, headingClass, detail) => { const node = make('span', 'flow-cell'), strong = make('strong', 'clamp2' + headingClass, heading); strong.title = heading; node.append(strong); return { node, detail: () => { if (detail) { const d = make('span', '', detail); d.title = detail; node.append(d); } } }; };
      const proposedCell = cell(s.proposed.title, s.proposed.id ? ' id' : '', s.proposed.detail), proposed = proposedCell.node;
      if (Number.isFinite(s.proposed.probability)) { const prob = make('span', 'prob'), bar = make('span', 'prob-bar'), fill = make('i'); fill.style.width = Math.max(0, Math.min(100, s.proposed.probability * 100)) + '%'; bar.append(fill); prob.append(bar, make('b', '', (s.proposed.probability * 100).toFixed(0) + '%')); prob.title = 'Score reported for the chosen candidate; uncalibrated, not a correctness estimate'; proposed.append(prob); }
      proposedCell.detail();
      const arrow = make('span', 'flow-arrow' + (s.inFlight ? ' pending' : ''), s.inFlight ? '…' : '→');
      const happenedCell = cell(s.happened.title, '', s.happened.detail), happened = happenedCell.node; happenedCell.detail();
      const verdictCell = make('span', 'verdict-cell'); const verdict = make('span', 'verdict ' + toneClass(s.verdict.tone), s.verdict.label); verdict.title = s.verdict.note; verdictCell.append(verdict);
      const latency = make('span', 'flow-latency', s.scoring.value); latency.append(make('small', '', s.scoring.label));
      line.append(when, sessionWrap, proposed, arrow, happened, verdictCell, latency);
      const focusEvent = s.score || latest;
      const inspect = make('button', 'flow-inspect', 'Inspect');
      inspect.dataset.inspectId = focusEvent.event_id;
      inspect.setAttribute('aria-pressed', String(group.some(e => e.event_id === selected)));
      inspect.setAttribute('aria-label', 'Inspect decision at ' + clock(first) + ' in ' + sessionName(cfg, latest.session_id));
      inspect.onclick = e => { e.stopPropagation(); selected = focusEvent.event_id; render(); };
      verdictCell.append(inspect);
      for (const cell of [proposed, happened, verdictCell, when, latency]) { cell.style.cursor = 'pointer'; cell.onclick = () => { selected = focusEvent.event_id; render(); }; }
      // stage chain
      const stages = make('div', 'flow-stages');
      for (const e of group.filter(isStage)) {
        const info = describe(e, synthetic(e, keys)), stage = make('button', 'flow-stage ' + info.tone + (arrivals.has(e.event_id) ? ' arriving' : '') + (e.event_id === selected ? ' selected' : ''));
        stage.dataset.stageId = e.event_id; stage.setAttribute('aria-pressed', String(e.event_id === selected)); stage.title = clock(e) + ' · ' + info.source;
        stage.append(make('strong', '', info.title), make('span', '', info.detail));
        if (Number.isFinite(e.data.duration_ms)) stage.append(make('span', 'flow-duration', fmt(e.data.duration_ms)));
        stage.onclick = () => { selected = e.event_id; render(); }; stages.append(stage);
      }
      card.append(line, stages, make('p', 'flow-outcome', s.verdict.note));
      $('flowCards').append(card);
      if (following && group.some(e => arrivals.has(e.event_id))) stages.scrollLeft = stages.scrollWidth;
    }
  }

  function render() {
    renderedAt = Date.now();
    if (source !== 'live') $('studyPanel').hidden = true;
    const focused = document.activeElement;
    const focusKey = ['eventId', 'sessionId', 'childrenId', 'stageId', 'runtimeSessionId', 'circuitEventId', 'inspectId'].find(key => focused?.dataset?.[key]);
    const focusValue = focusKey ? focused.dataset[focusKey] : null;
    const keys = scriptedKeys(events), base = currentWindow(), realBase = base.filter(e => !synthetic(e, keys));
    const sessions = renderSessions($('includeSynthetic').checked ? base : realBase, keys);
    const scoped = scopeData(base), hidden = scoped.filter(e => synthetic(e, keys)).length;
    const real = scoped.filter(e => !synthetic(e, keys)), m = metrics(real);
    put('modelCount', m.scores + m.judged); put('fastCount', m.cheapCalls); put('usualCount', m.hostCalls); put('switchedCount', m.switched); put('toolCount', m.tools); put('errorCount', m.issues); put('fastExecuted', m.fastExecuted); put('scorerP95', fmt(m.p95));
    document.querySelector('.kpis')?.classList.toggle('has-shortcut', m.fast > 0 || m.bypassed > 0);
    put('matchCount', m.comparisons.length ? m.comparisons.filter(e => e.data.agreement === 'match').length + '/' + m.comparisons.length : '—');
    $('errorCount').closest('.kpi').classList.toggle('has-issues', m.issues > 0);
    put('impactDetail', m.fastExecuted ? m.fastExecuted + ' tool execution' + (m.fastExecuted === 1 ? '' : 's') + ' confirmed after a fast submission.' : m.fast ? 'Fast actions were submitted; no completed fast-path execution is recorded.' : 'No executed fast path in this window; shadow proposals never change execution.');
    put('qualityEvidence', m.failedFast ? m.failedFast + ' fast execution' + (m.failedFast === 1 ? '' : 's') + ' failed.' : '');
    put('scopeNotice', 'Scripted and demo events are excluded; tool results may be hook observations.');
    const cfg = session ? configFor(events, session) : null;
    const current = session ? sessions.find(s => s.id === session) : null;
    put('viewTitle', session ? sessionName(cfg, session) : 'All sessions');
    put('viewSubtitle', session ? [cfg.mode ? backendName(cfg.backend, cfg.backend_label) + ' · ' + cfg.mode + ' mode' : 'Configuration not recorded', current?.state, shortId(session) !== sessionName(cfg, session) ? session : ''].filter(Boolean).join(' · ') : 'Runtime activity from ' + sessions.filter(s => !s.parent && s.reporting).length + ' session(s) currently reporting.');
    put('syntheticCount', hidden ? '(' + hidden + ($('includeSynthetic').checked ? ' shown)' : ' hidden)') : '');
    const display = scoped.filter(e => ($('includeSynthetic').checked || !synthetic(e, keys)) && useful(e) && (category === 'decisions' ? ['requested', 'scored', 'routed', 'shadow_proposed', 'shadow_observed', 'shadow_agreement', 'role_proposed', 'role_agreement', 'fallback'].includes(kind(e)) : category === 'errors' ? issue(e) : true));
    const rows = display.slice(-250).reverse(); $('feed').replaceChildren();
    if (selected && !scoped.some(e => e.event_id === selected && ($('includeSynthetic').checked || !synthetic(e, keys)))) selected = null;
    for (const e of rows) {
      const info = describe(e, synthetic(e, keys)), button = make('button', 'event-row' + (selected === e.event_id ? ' selected' : ''));
      button.setAttribute('aria-pressed', String(selected === e.event_id)); button.dataset.eventId = e.event_id;
      const icon = make('span', 'event-icon ' + info.tone, info.tone === 'error' ? '!' : info.tone === 'decision' ? '◆' : info.tone === 'synthetic' ? '~' : '·'), body = make('span');
      body.append(make('span', 'event-title', info.title), make('span', 'event-description', info.detail));
      const when = make('span', 'event-time', clock(e)); if (Number.isFinite(e.data.duration_ms)) when.append(make('span', 'event-duration', fmt(e.data.duration_ms)));
      const sessionCfg = configFor(events, e.session_id);
      button.append(icon, body, make('span', 'event-source', info.source), make('span', 'event-session', sessionName(sessionCfg, e.session_id)), when);
      button.onclick = () => { selected = e.event_id; render(); }; $('feed').append(button);
    }
    $('emptyState').hidden = category === 'live' || rows.length > 0; $('feed').hidden = category === 'live' || !rows.length; $('feedHeading').hidden = category === 'live' || !rows.length;
    put('emptyTitle', connectionError ? 'Connection needs attention' : hidden ? 'Scripted events are hidden' : category === 'errors' ? 'No issues recorded' : 'No activity in this window');
    put('emptyDetail', connectionError ? 'Restore the viewer connection above. Existing records remain available.' : hidden ? 'Enable scripted & demo events to inspect them. They never count as model decisions or measured improvements.' : source !== 'live' ? 'This is a saved trace. Try another category or session.' : 'The viewer is listening. Start a turn in a session with the bundle loaded, or widen the time window.');
    put('feedCount', category === 'live' ? display.length + ' events in window' : display.length > 250 ? 'Latest 250 of ' + display.length + ' events' : display.length + ' events');
    put('dataHealth', invalidLines ? invalidLines + ' invalid records skipped' : windowGap ? 'Older events expired from the retained window' : 'No prompts, tool contents, or private reasoning.');
    put('freshness', source !== 'live' ? 'Saved trace · no live updates' : lastPoll ? 'Checked ' + age(lastPoll) + ' · last event ' + (events.length ? age(stamp(events.at(-1))) : 'not received') : 'Waiting for first response');
    put('followBtn', source !== 'live' ? 'Return to live' : following ? 'Following live' : 'Resume live'); $('followBtn').setAttribute('aria-pressed', String(source === 'live' && following));
    put('bypassedCount', m.bypassed);
    renderFlows(scoped.filter(e => $('includeSynthetic').checked || !synthetic(e, keys)), keys);
    renderDetails(events.find(e => e.event_id === selected) || circuitFocus, keys);
    if (source !== 'live') { put('connection', 'Saved trace'); $('connection').className = 'connection replay'; }
    else if (connectionError) { put('connection', 'Disconnected'); $('connection').className = 'connection problem'; }
    else { put('connection', lastPoll ? (hasMore ? 'Syncing history' : following ? 'Connected' : 'Connected · paused') : 'Connecting'); $('connection').className = 'connection' + (lastPoll ? ' connected' : ''); }
    $('connectionNotice').hidden = !connectionError; put('connectionNotice', connectionError);
    if (focusKey) { const replacement = [...document.querySelectorAll('button')].find(node => node.dataset[focusKey] === focusValue); replacement?.focus({ preventScroll: true }); }
  }

  $('circuitLatest').onclick = () => { selected = null; render(); };
  $('allSessions').onclick = () => selectSession(''); $('sessionSearch').oninput = render;
  for (const id of ['timeWindow', 'includeSynthetic', 'includeChildren']) $(id).onchange = () => { selected = null; render(); };
  document.querySelectorAll('[data-filter]').forEach(b => { b.onclick = () => { category = b.dataset.filter; document.querySelectorAll('[data-filter]').forEach(x => { x.classList.toggle('active', x === b); x.setAttribute('aria-pressed', String(x === b)); }); render(); }; });
  $('followBtn').onclick = () => { if (source !== 'live') { pollGeneration++; source = 'live'; events = []; seen.clear(); cursor = 0; epoch = null; historyReady = false; selected = null; session = ''; following = true; frozenIds = null; $('timeWindow').value = '3600000'; schedule(0); } else { following = !following; frozenIds = following ? null : new Set(events.map(e => e.event_id)); } render(); };
  $('loadBtn').onclick = () => $('fileInput').click();
  $('fileInput').onchange = async () => { try { const batch = []; for (const file of $('fileInput').files) { const text = await file.text(); batch.push(...(text.trim().startsWith('[') ? JSON.parse(text) : text.split(/\r?\n/).filter(Boolean).map(JSON.parse))); } pollGeneration++; clearTimeout(pollTimer); source = 'file'; events = []; seen.clear(); selected = null; session = ''; frozenIds = null; following = false; connectionError = ''; $('reconnectPanel').hidden = true; $('timeWindow').value = 'all'; ingest(batch); } catch (_) { connectionError = 'Could not read this trace. Choose a JSON array or a JSONL event file.'; render(); } };
  $('exportBtn').onclick = () => { const keys = scriptedKeys(events), data = scopeData(currentWindow()).filter(e => $('includeSynthetic').checked || !synthetic(e, keys)), blob = new Blob([data.map(e => JSON.stringify(e)).join('\n') + '\n'], { type: 'application/x-ndjson' }), a = document.createElement('a'); a.href = URL.createObjectURL(blob); a.download = 'amplifier-events.jsonl'; a.click(); setTimeout(() => URL.revokeObjectURL(a.href), 1000); };
  $('connectForm').onsubmit = e => { e.preventDefault(); try { const url = new URL($('viewerLink').value); if (url.origin !== location.origin) throw Error(); const value = new URLSearchParams(url.hash.slice(1)).get('token'); if (!value) throw Error(); pollGeneration++; token = value; try { sessionStorage.setItem(tokenKey, token); } catch (_) { } $('viewerLink').value = ''; connectionError = ''; $('reconnectPanel').hidden = true; schedule(0); } catch (_) { connectionError = 'Use the full viewer link for this same local address and port.'; render(); } };
  function schedule(delay) { clearTimeout(pollTimer); pollTimer = setTimeout(poll, delay); }
  window.addEventListener('hashchange', () => {
    const value = new URLSearchParams(location.hash.slice(1)).get('token');
    if (!value) return;
    token = value; pollGeneration++;
    try { sessionStorage.setItem(tokenKey, token); } catch (_) { }
    history.replaceState(null, '', location.pathname + location.search);
    connectionError = ''; schedule(0);
  });
  let lastEfficiencyPoll = 0;
  async function pollEfficiency() {
    if (Date.now() - lastEfficiencyPoll < 30000 || source !== 'live') return;
    lastEfficiencyPoll = Date.now();
    try {
      const response = await fetch('/api/efficiency', { credentials: 'same-origin', headers: token ? { Authorization: 'Bearer ' + token } : {}, signal: AbortSignal.timeout(20000) });
      if (!response.ok || source !== 'live') return;
      const v = efficiencyView(await response.json());
      put('effCost', v.cost); put('effCostDetail', v.costDetail); put('effTime', v.time); put('effTimeDetail', v.timeDetail);
      put('effCalls', v.calls); put('effCallsDetail', v.callsDetail); put('effNote', v.note);
      const row = (cells) => { const r = make('div', 'savings-project'); cells.forEach((c, i) => r.append(make(i ? 'span' : 'b', '', c))); return r; };
      $('effLevers').replaceChildren(row(['Lever', 'Receipts · calls', 'Cost', 'Time']), ...v.levers.map(l => row([l.name, l.active ? l.receipts + ' · ' + l.calls + ' calls' : 'not active yet', l.active ? l.usd : '—', l.active ? l.secs : '—'])));
      $('effProjects').replaceChildren(...(v.projects.length ? [row(['Project', 'Receipts · calls', 'Cost', 'Time'])] : []), ...v.projects.map(p => row([p.name, p.receipts + ' · ' + p.calls + ' calls', p.usd, p.secs])));
      const byHarness = $('effHarness');
      if (byHarness) byHarness.replaceChildren(...(v.harnesses.length > 1 || (v.harnesses[0] && v.harnesses[0].name !== 'Amplifier') ? [row(['Harness', 'Receipts · calls', 'Cost', 'Time']), ...v.harnesses.map(h => row([h.name, h.receipts + ' · ' + h.calls + ' calls', h.usd, h.secs]))] : []));
    } catch (_) { /* keep last values */ }
  }
  let lastSavingsPoll = 0;
  async function pollSavings() {
    if (Date.now() - lastSavingsPoll < 60000 || source !== 'live') return;
    lastSavingsPoll = Date.now();
    try {
      const response = await fetch('/api/savings', { credentials: 'same-origin', headers: token ? { Authorization: 'Bearer ' + token } : {}, signal: AbortSignal.timeout(20000) });
      if (!response.ok || source !== 'live') return;
      renderSavings(await response.json());
    } catch (_) { /* the panel keeps its last values */ }
  }
  function renderSavings(r) {
    const view = savingsView(r);
    $('savingsPanel').hidden = false;
    put('savingsScope', view.scope);
    put('savingsCost', view.cost); put('savingsCostDetail', view.costDetail);
    put('savingsTime', view.time); put('savingsTimeDetail', view.timeDetail);
    put('savingsTurns', view.turns); put('savingsTurnsDetail', view.turnsDetail);
    put('savingsNote', view.note);
    const list = $('savingsProjects');
    if (list) list.replaceChildren(...(view.projects || []).map(p => { const row = make('div', 'savings-project'); row.append(make('b', '', p.name), make('span', '', p.turns), make('span', '', p.saved), make('small', '', p.why)); return row; }));
    const days = (r.by_day || []).slice(-30), peak = Math.max(0, ...days.map(d => d.saved_usd || 0));
    $('savingsDays').replaceChildren(...days.map(d => { const bar = make('i'); bar.style.height = peak > 0 ? Math.max(2, Math.round(40 * Math.max(0, d.saved_usd) / peak)) + 'px' : '2px'; bar.title = d.day + ': $' + (d.saved_usd || 0).toFixed(2) + ' · ' + d.cheap_turns + ' cheaper / ' + d.strong_turns + ' host'; return bar; }));
  }
  async function pollStudy() {
    if (Date.now() - lastStudyPoll < 5000 || source !== 'live') return;
    lastStudyPoll = Date.now();
    try {
      const response = await fetch('/api/study', { credentials: 'same-origin', headers: token ? { Authorization: 'Bearer ' + token } : {}, signal: AbortSignal.timeout(5000) });
      if (!response.ok) return;
      const study = await response.json();
      if (source !== 'live') return;
      $('studyPanel').hidden = study.status === 'not_configured';
      if (!study.available) { put('studyStatus', 'Data unavailable'); return; }
      put('studyProgress', study.completed + ' / ' + study.planned + ' runs');
      put('studyStatus', pretty(study.status));
      $('studyMeter').max = study.planned; $('studyMeter').value = study.completed;
      put('studyNote', study.complete ? 'Collection complete. Event counts alone do not establish savings or quality.' : 'Collection incomplete. These counts include failures and non-use; they are not a final performance verdict.');
      $('studyRows').replaceChildren();
      for (const arm of study.arms) {
        const row = make('tr');
        const seconds = Number.isFinite(arm.median_seconds) ? Math.round(arm.median_seconds) + ' s' : '—';
        for (const value of [arm.harness + ' / ' + arm.arm, arm.completed + '/' + arm.planned, arm.passed, seconds, arm.fd_requests]) row.append(make('td', '', String(value)));
        $('studyRows').append(row);
      }
    } catch (_) { put('studyStatus', 'Update unavailable'); }
  }
  async function poll() {
    if (source !== 'live') return;
    const generation = ++pollGeneration;
    try {
      const response = await fetch('/api/events?after=' + cursor, { credentials: 'same-origin', headers: token ? { Authorization: 'Bearer ' + token } : {}, signal: AbortSignal.timeout(5000) });
      if (source !== 'live' || generation !== pollGeneration) return;
      if (!response.ok) { if (response.status === 401) { $('reconnectPanel').hidden = false; throw Error('The viewer link has expired or is missing its access token. Reopen the launching link.'); } throw Error('The local viewer returned HTTP ' + response.status + '. Retrying…'); }
      const payload = await response.json();
      if (source !== 'live' || generation !== pollGeneration) return;
      if (epoch && epoch !== payload.epoch) { events = []; seen.clear(); cursor = 0; epoch = payload.epoch; historyReady = false; selected = null; windowGap = false; schedule(0); return; }
      if (cursor && payload.first_cursor > cursor + 1) windowGap = true;
      const repaint = !lastPoll || !!connectionError || hasMore !== !!payload.has_more;
      epoch = payload.epoch; cursor = payload.cursor; retained = payload.retained; invalidLines = payload.invalid_lines || 0; hasMore = !!payload.has_more; lastPoll = Date.now(); connectionError = ''; $('reconnectPanel').hidden = true; ingest(payload.events || [], repaint);
      if (!hasMore) historyReady = true;
      void pollStudy(); void pollSavings(); void pollEfficiency();
    } catch (error) { if (source !== 'live' || generation !== pollGeneration) return; connectionError = error.name === 'TimeoutError' ? 'The viewer did not respond within 5 seconds. Retrying…' : error instanceof TypeError ? 'Cannot reach the local viewer. Retrying…' : error.message; render(); }
    if (source === 'live' && generation === pollGeneration) schedule(connectionError ? 2000 : hasMore ? 0 : 500);
  }
  // Refresh relative ages and in-flight state even when no event arrives.
  setInterval(() => { if (source === 'live' && following && Date.now() - renderedAt > 4000) render(); }, 5000);
  if (window.AFAST_EMBEDDED) { source = 'embedded'; following = false; $('timeWindow').value = 'all'; ingest(window.AFAST_EMBEDDED); }
  else { render(); schedule(0); }
})();
