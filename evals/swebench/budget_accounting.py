"""Budget holds for evidenced Jev timeouts; never turn missing usage into a cost."""
import hashlib
import json
import math


def money(value):
    return type(value) in (int, float) and math.isfinite(value) and value >= 0


def account(root, manifest, result):
    cost = result.get('cost_usd')
    if money(cost):
        return {'known_usd': cost, 'held_usd': 0., 'unknown_requests': 0}
    hold = manifest.get('unknown_jev_timeout_hold_usd')
    if cost is not None or not money(hold) or hold < 10 or result.get('infrastructure_failure'):
        return None
    if not all(money(result.get(k)) for k in ('provider_cost_usd', 'retrieval_cost_usd')):
        return None
    if result.get('judge_cost_usd') is not None or not result.get('session_id'):
        return None
    name = result.get('name')
    if name not in manifest.get('runs', {}):
        return None
    requested, scored, timed_out, seen = set(), {}, set(), set()
    evidence = hashlib.sha256()
    for path in sorted((root/'runs'/name/'events').glob('*.jsonl')):
        raw = path.read_bytes(); evidence.update(raw)
        for line in raw.splitlines():
            try:
                event = json.loads(line)
            except ValueError:
                return None
            if event.get('session_id') != result['session_id']:
                continue
            if event.get('event_id') in seen:
                continue
            if event.get('event_id'):
                seen.add(event['event_id'])
            data = event.get('data') or {}
            if data.get('backend') != 'jev':
                continue
            kind, decision = event.get('event'), event.get('decision_id')
            if data.get('reason_code') == 'backend_error' or event.get('synthetic'):
                return None
            if kind in ('fast_decisions:requested', 'fast_decisions:scored') or data.get('reason_code') == 'decision_timeout':
                if not decision:
                    return None
                if kind == 'fast_decisions:requested':
                    requested.add(decision)
                elif kind == 'fast_decisions:scored':
                    tokens = data.get('input_tokens')
                    if type(tokens) is not int or tokens < 0 or decision in scored:
                        return None
                    scored[decision] = tokens
                elif kind == 'fast_decisions:fallback':
                    timed_out.add(decision)
    # Only complete, attributable timeout evidence is eligible for a hold.
    # Other missing usage and unclassified failures still stop the campaign.
    unknown = timed_out - scored.keys()
    if not unknown or requested != scored.keys() | timed_out:
        return None
    known = result['provider_cost_usd'] + result['retrieval_cost_usd'] + sum(scored.values())*.042/1e6
    return {'known_usd': known, 'held_usd': hold*len(unknown),
            'unknown_requests': len(unknown), 'decision_ids': sorted(unknown),
            'events_sha256': evidence.hexdigest(), 'method': 'timeout_hold_not_billed_cost'}


def ledger(root, manifest, results):
    rows = {}
    for index, result in enumerate(results):
        entry = account(root, manifest, result)
        if entry is None:
            raise ValueError('Unknown prior cost without an evidenced timeout hold')
        rows[result.get('name', str(index))] = entry
    prior = manifest.get('prior_cost_usd', 0.)
    if not money(prior):
        raise ValueError('Invalid prior cost')
    known = prior + sum(r['known_usd'] for r in rows.values())
    held = sum(r['held_usd'] for r in rows.values())
    return {'known_cost_usd': known, 'held_cost_usd': held,
            'budget_accounted_usd': known+held,
            'unknown_requests': sum(r['unknown_requests'] for r in rows.values()), 'runs': rows}
