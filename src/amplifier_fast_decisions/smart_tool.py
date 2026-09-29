"""Portable advisory selection, bounded source retrieval, and UI proposals. No specific coding-agent
harness (Claude Code, Codex, OpenCode, Amplifier, ...) is required.

The caller supplies bounded task/context data and eligible read/list targets.
Selection never reads or executes a target. Search reads bounded eligible source;
CUA proposes actions and leaves all UI execution to the host.
"""
from __future__ import annotations

import asyncio
from dataclasses import asdict, dataclass, field
from importlib import metadata, resources
import json
import os
from pathlib import Path, PurePosixPath
import re
import time
from typing import Any
from uuid import uuid4

from .contracts import Candidate, DecisionRequest, SLOW
from .backends import DEFAULT_JEV_MODEL, JevBackend
from .local_backend import LayaBackend, OllamaBackend, PROBABILITY_KIND
from .privacy import scrub
from .telemetry import Emitter, JsonlRecorder

MAX_INPUT_BYTES = 16384
DEFAULT_EVENTS = Path.home() / '.amplifier' / 'fast-decisions' / 'events'
CAPABILITIES = {
    'manifest': ('deterministic', 'Read the installed tool manifest as JSON.'),
    'describe': ('deterministic', 'Describe the input schema, result and calling contract.'),
    'install-skill': ('deterministic', 'Install a minimal Agent Skill for selected local harnesses.'),
    'diagnose': ('deterministic', 'Inspect installation, session integration, local model and viewer health.'),
    'measure': ('deterministic', 'Count observed provider and tool executions across a session tree.'),
    'compare': ('deterministic', 'Compare matched baseline/enabled runs with explicit outcome checks.'),
    'select': ('model-backed', 'Suggest one caller-supplied read/list target, or abstain.'),
    'search': ('model-backed', 'Retrieve bounded source windows using upstream Jevgrep (Laya is experimental).'),
    'cua': ('model-backed', 'Propose an action on observed UI controls; the host owns all execution.'),
}

HARNESSES = frozenset({'amplifier', 'claude', 'codex', 'copilot', 'cursor', 'gemini', 'grok', 'opencode', 'other'})
SKILL_HOSTS = {'codex': '.agents', 'claude': '.claude', 'amplifier': '.amplifier', 'opencode': '.config/opencode'}


def agent_skill() -> str:
    """Generate the minimal discovery skill from canonical manifest identity."""
    info = manifest()
    return ('---\n' + f'name: {json.dumps(info["name"])}\n'
            + f'description: {json.dumps(info["description"])}\n---\n\n'
            + 'Install the CLI if needed:\n\n```bash\n'
            + "uv tool install 'amplifier-fast-decisions[local] @ git+https://github.com/michaeljabbour/amplifier-bundle-fast-decisions@main'\n```\n\n"
            + f'Run `{info["name"]} --help` and follow its guidance. Confirm capability\n'
            + f'arguments with `{info["name"]} <capability> --help` before use.\n')


def install_skill(host: str, *, home: str | Path | None = None) -> dict[str, Any]:
    """Add a discovery skill, refusing changed existing files before any writes.

    Resolves/deduplicates symlinked host directories. Existing identical content
    is unchanged. ``home`` is a library seam for isolated installation/tests.
    No host configuration, approvals, or model services are modified.
    """
    if host not in {*SKILL_HOSTS, 'all'}:
        raise ValueError('Choose codex, claude, amplifier, opencode, or all.')
    base = Path(home).expanduser() if home is not None else Path.home()
    selected = list(SKILL_HOSTS) if host == 'all' else [host]
    name, content = manifest()['name'], agent_skill()
    destinations: dict[Path, list[str]] = {}
    for value in selected:
        target = (base / SKILL_HOSTS[value] / 'skills' / name / 'SKILL.md').resolve()
        destinations.setdefault(target, []).append(value)
    # Preflight *all* destinations before writing to any of them.
    for target in destinations:
        if target.exists():
            if not target.is_file() or target.read_text(encoding='utf-8') != content:
                raise ValueError(f'Existing skill differs; preserved {target}. Review it before installing.')
        for ancestor in target.parents:
            if ancestor.exists():
                if not ancestor.is_dir():
                    raise ValueError(f'Skill parent is not a directory: {ancestor}')
                break
    created: list[Path] = []
    rows = []
    try:
        for target, hosts in destinations.items():
            if target.exists():
                # Recheck after preflight in case another process changed it.
                if target.read_text(encoding='utf-8') != content:
                    raise ValueError(f'Existing skill changed; preserved {target}.')
                status = 'unchanged'
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                with target.open('x', encoding='utf-8') as stream:
                    created.append(target)
                    stream.write(content)
                status = 'installed'
            rows.append({'hosts': hosts, 'path': str(target), 'status': status})
    except (OSError, ValueError):
        # Remove only this invocation's files, never pre-existing files.
        for target in created:
            try:
                if target.read_text(encoding='utf-8') == content:
                    target.unlink()
            except OSError:
                pass
        raise
    return {'host': host, 'skills': rows, 'installed': len(created),
            'message': 'Skill discovery only. Ensure the CLI is on PATH; refresh the harness skill catalog if needed.'}


def manifest() -> dict[str, Any]:
    """Read the single packaged manifest; importing the tool needs no model."""
    text = resources.files('amplifier_fast_decisions').joinpath('SMART_TOOL.md').read_text(encoding='utf-8')
    _, front, body = text.split('---', 2)
    return {**json.loads(front), 'body': body.strip()}


def describe() -> dict[str, Any]:
    """Return the JSON contract. Additional keys are rejected, not interpreted."""
    return {
        'name': manifest()['name'], 'capability': 'select', 'model_backed': True,
        'effect': 'advisory_only', 'executes_actions': False,
        'input_schema': {
            'type': 'object', 'additionalProperties': False,
            'required': ['task', 'candidates'],
            'properties': {
                'task': {'type': 'string', 'minLength': 1, 'maxLength': 1800},
                'context': {'type': 'string', 'maxLength': 1800},
                'session_id': {'type': 'string', 'pattern': '^[A-Za-z0-9_-]{1,128}$'},
                'parent_session_id': {'type': 'string', 'pattern': '^[A-Za-z0-9_-]{1,128}$'},
                'harness': {'type': 'string', 'enum': sorted(HARNESSES)},
                'candidates': {'type': 'array', 'minItems': 1, 'maxItems': 12,
                    'items': {'type': 'object', 'additionalProperties': False,
                        'required': ['id', 'operation', 'path'], 'properties': {
                            'id': {'type': 'string', 'pattern': '^[A-Za-z][A-Za-z0-9_-]{0,63}$'},
                            'operation': {'enum': ['read', 'list']},
                            'path': {'type': 'string', 'minLength': 1, 'maxLength': 512}}}},
            },
        },
        'constraints': ['At most 1800 combined task/context characters.',
                       'Candidate IDs must be unique; reason is reserved.',
                       'Paths are relative, non-hidden, without parent traversal or backslashes.',
                       'The backend may abstain when the combined prompt exceeds its byte bound.'],
        'result': {'ok': 'false for invalid input, unavailable/invalid model, or telemetry failure',
                   'status': 'selected or abstain', 'choice': 'offered candidate ID or null',
                   'reason_code': 'machine-readable outcome', 'backend': 'actual backend identity, such as ollama-token or jev',
                   'probabilities': 'native backend scores; inspect probability_kind, not calibrated accuracy',
                   'duration_ms': 'scoring wall time; excludes CLI startup and recording shutdown',
                   'option_set_hash': 'order-sensitive hash of model-facing options and ID bindings; null if unavailable',
                   'confidence_kind': 'not_reported for the local token scorer; not empirical calibration',
                   'effect': 'advisory_only; no execution or savings established'},
        'exit_codes': {'0': 'selected or normal model/policy abstention', '1': 'failed capability',
                       '2': 'invalid command line or JSON'},
    }


def skill(capability: str | None = None) -> str:
    """Render installed Agent Skill guidance; both CLI and library use this."""
    info = manifest()
    if capability is not None and capability not in CAPABILITIES:
        raise ValueError('Unknown capability')
    name = info['name'] + (f'-{capability}' if capability else '')
    lines = [f'<skill_content name="{name}">', f'Skill directory: {Path(__file__).resolve().parent}']
    try:
        urls = metadata.metadata('amplifier-fast-decisions').get_all('Project-URL') or []
        repository = next((u.split(',', 1)[1].strip() for u in urls if u.lower().startswith('repository,')), None)
        if repository:
            lines.append(f'Repository: {repository}')
    except metadata.PackageNotFoundError:
        pass
    lines += ['Relative paths are relative to the skill directory.', '', f'# {name}', '']
    if capability is None:
        lines += [info['body'], '', '## Capabilities', '',
                  f'Each capability has a skill: `{info["name"]} <capability> --help`.']
        lines += [f'- `{key}` [{kind}] — {summary}' for key, (kind, summary) in CAPABILITIES.items()]
    elif capability in {'manifest', 'describe'}:
        lines += [CAPABILITIES[capability][1], 'Deterministic; no arguments or provider required.',
                  f'Example: `{info["name"]} {capability}`.',
                  'Result: one JSON object on stdout. Exit 0 on success; invalid flags exit 2.']
    elif capability in {'diagnose', 'measure', 'compare'}:
        lines += [
            CAPABILITIES[capability][1],
            'No inference or configuration changes. Results are JSON on stdout; errors on stderr.',
            'diagnose/measure: --events DIRECTORY_OR_JSONL and optional --session PARENT_ID (includes children).',
            'diagnose: optional --state-file FILE, --ollama-url LOOPBACK_ORIGIN, --model NAME, --offline.',
            'Without --offline, diagnose probes local Ollama and authenticated viewer health; no model call.',
            'compare: --input FILE in paired-runs-v1 format. Relative receipt paths resolve beside FILE.',
            'See docs/OPERATIONS.md for the schema, commands and evidence limits.',
            'Exit 0 means report produced; inspect statuses and eligibility, not only the exit code.',
            'Invalid input exits 2. Missing telemetry is unknown, not proof of zero activity.',
            'Library equivalents: operations.diagnose(), operations.measure(), operations.compare(payload, base_dir=...).',
        ]
    elif capability == 'install-skill':
        lines += [
            'Deterministic installation of a minimal discovery skill; no model is used.',
            'Required argument: --host codex|claude|amplifier|opencode|all.',
            'Example: amplifier-fast-decisions install-skill --host all',
            'Writes SKILL.md below ~/.agents/skills, ~/.claude/skills, ~/.amplifier/skills, or ~/.config/opencode/skills.',
            'All destinations are checked first. Identical files are unchanged; modified existing files',
            'cause failure before any writes. Symlink aliases resolving to the same destination are deduplicated.',
            'Result: JSON paths, host names, and installed/unchanged status. Exit 0 on success,',
            '1 for a conflict/filesystem error, 2 for bad arguments. No force/overwrite option exists.',
            'The library equivalent is install_skill(host, home=None). Set home to isolate tests.',
            'No host settings, permissions, or provider pipelines are changed. Refresh skill discovery if needed.',
        ]
    elif capability in {'search', 'cua'}:
        lines += [CAPABILITIES[capability][1],
            '--input FILE (or - for stdin) supplies JSON. --backend laya|jev defaults to jev.',
            '--laya-url URL optionally overrides the Laya endpoint; loopback is the default.',
            '--allow-external-state is required for remote judges and upstream Jevgrep.',
            'search: input {"query":"where is retry logic?","path":"."}; --root DIRECTORY bounds reads.',
            'search: --timeout-ms defaults to 60000; at most 32 KiB eligible source, ignore rules enforced.',
            'Laya retrieval is a bounded local relevance implementation, not the upstream Jevgrep algorithm.',
            'cua: input {"goal":"open Reports","snapshot":{"surface_id":"demo","revision":"1",',
            '"text":"Home","elements":[{"id":"reports","label":"Reports","operations":["CLICK"]}]}}.',
            'cua: --timeout-ms defaults to 3000; --min-probability defaults to .75.',
            'CUA only proposes. Reobserve and validate freshness, obtain native approval, execute with host tools,',
            'then verify the result. DONE never proves completion by itself. Low scores yield reason.',
            'No images, invented targets, input text generation, or autonomous desktop access.',
            'Exit 0: result produced; 1: backend failure/disabled; 2: invalid input. Inspect status for abstention.',
            'Library: await search(payload, root=...) or await cua(payload) from smart_tool.',
        ]
    else:
        lines += [
            'Model-backed advisory selection. Use only for an already bounded read/list choice.',
            'Arguments: --input FILE reads a UTF-8 JSON object; --input - reads stdin.',
            'Without --input, stdin must be piped; an interactive terminal fails without prompting.',
            '--backend laya|local|ollama|jev overrides FAST_DECISIONS_JUDGE (default jev).',
            '--laya-url URL overrides FAST_DECISIONS_LAYA_URL (default http://127.0.0.1:8090).',
            '--model NAME defaults to qwen3:0.6b locally, or TYPESAFE_DEFAULT_MODEL / jev-1.13.0 for Jev.',
            '--ollama-url ORIGIN overrides FAST_DECISIONS_OLLAMA_URL (default http://127.0.0.1:11434).',
            '--allow-external-state / --no-allow-external-state overrides FAST_DECISIONS_ALLOW_EXTERNAL_STATE.',
            'Jev requires explicit external-state consent and TYPESAFE_API_KEY in the environment.',
            '--timeout-ms INTEGER defaults to 500 (10–500); --events DIRECTORY sets the metadata recorder location.',
            'Use describe for the complete task/context/candidates/session/parent/harness input schema.',
            'Only task and optional context content are provided as observations. Candidate targets are data.',
            'Example: amplifier-fast-decisions select --input request.json --timeout-ms 500',
            'Result: one JSON object with ok, status, choice, reason_code, model, probabilities,',
            'probability_kind, confidence_kind, option_set_hash, duration_ms, session_id, parent_session_id, decision_id, effect and remediation.',
            'A selected choice is advisory. It grants no permission and executes nothing.',
            'Low scores and model abstention are normal results (exit 0). Invalid/unsupported input,',
            'unavailable/invalid model, or recording failure returns typed abstention with ok=false (exit 1).',
            'Bad command-line syntax or JSON exits 2. Diagnostics use stderr; results use stdout.',
            'Start Ollama, pull qwen3:0.6b, and warm the model before latency-sensitive calls.',
            'The call deadline includes model queue wait. It does not include process startup or telemetry flush.',
            'Local calls require no key. Jev sends bounded task/context and candidate descriptions externally.',
            'No key is read from a repository file. No backend or deterministic scoring fallback exists.',
            'AFAST_EVENTS_DIR sets the recorder directory when --events is omitted.',
            'For composition, call await select(payload) from amplifier_fast_decisions.smart_tool.',
        ]
    return '\n'.join(lines + ['', '<skill_resources><file>SMART_TOOL.md</file><file>smart_tool.py</file></skill_resources>', '</skill_content>'])


@dataclass(frozen=True)
class Selection:
    """Typed suggestion. ``ok=False`` is a failure, never a successful shortcut."""
    ok: bool
    status: str
    reason_code: str
    session_id: str
    parent_session_id: str | None
    decision_id: str
    choice: str | None = None
    model: str | None = None
    backend: str | None = None
    probabilities: dict[str, float] = field(default_factory=dict)
    probability_kind: str = 'not_reported'
    duration_ms: float = 0.0
    effect: str = 'advisory_only'
    remediation: str | None = None
    confidence_kind: str = 'not_reported'
    option_set_hash: str | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _validated(payload: Any) -> tuple[DecisionRequest, str, str | None, str]:
    if not isinstance(payload, dict) or set(payload) - {'task', 'context', 'candidates', 'session_id', 'parent_session_id', 'harness'}:
        raise ValueError('Unsupported request fields')
    if len(json.dumps(payload, ensure_ascii=False).encode('utf-8')) > MAX_INPUT_BYTES:
        raise ValueError('Request too large')
    task, context = payload.get('task'), payload.get('context', '')
    if not isinstance(task, str) or not task.strip() or not isinstance(context, str) or len(task) + len(context) > 1800:
        raise ValueError('Task/context must be bounded strings')
    session = payload.get('session_id', 'portable-' + uuid4().hex)
    parent = payload.get('parent_session_id')
    if any(not isinstance(v, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,128}', v) for v in [session] + ([parent] if parent is not None else [])) or parent == session:
        raise ValueError('Invalid session identity')
    harness = payload.get('harness', 'other')
    if harness not in HARNESSES:
        raise ValueError('Unsupported harness label')
    rows = payload.get('candidates')
    if not isinstance(rows, list) or not 1 <= len(rows) <= 12:
        raise ValueError('Expected 1–12 candidates')
    candidates = []
    for i, row in enumerate(rows):
        if not isinstance(row, dict) or set(row) != {'id', 'operation', 'path'}:
            raise ValueError('Expected id, operation and path only')
        path = row['path']
        if row['operation'] not in {'read', 'list'} or not isinstance(path, str) or not path or len(path) > 512:
            raise ValueError('Unsupported target')
        if '\\' in path or ':' in path or any(ord(c) < 32 for c in path) or PurePosixPath(path).is_absolute() or any(p.startswith('.') for p in path.split('/') if p != '.'):
            raise ValueError('Unsupported path')
        candidates.append(Candidate(row['id'], f"{row['operation']} {path}", 'fast_workspace', {'operation': row['operation'], 'path': path}, origin='portable_caller'))
    if len({c.id for c in candidates}) != len(candidates):
        raise ValueError('Duplicate candidate IDs')
    observations = [{'role': 'user', 'text': scrub(task, 1800)}]
    if context:
        observations.append({'role': 'tool', 'text': scrub(context, 1800)})
    return DecisionRequest(state={'observations': observations}, candidates=tuple(candidates)), session, parent, harness


async def select(payload: dict[str, Any], *, model: str | None = None,
                 backend: str | None = None, allow_external_state: bool | None = None,
                 ollama_url: str | None = None, laya_url: str | None = None, timeout_ms: int = 500,
                 events_dir: str | Path | None = None, _backend: Any = None) -> Selection:
    """Score bounded caller data without reading/executing targets.

    Uses native token probabilities, score >= .90 and margin >= .20. Optional
    context is content in payload, never a file reference. Missing model or bad
    output produces a failed typed abstention. Cancellation propagates normally.
    ``_backend`` is a private test seam; callers select laya, local/ollama or jev.
    Explicit arguments override environment defaults. Consent is checked before
    constructing any external backend, including an external test seam.
    """
    session, parent, decision_id = 'portable-' + uuid4().hex, None, uuid4().hex
    try:
        request, session, parent, harness = _validated(payload)
        backend_name = backend if backend is not None else os.getenv('FAST_DECISIONS_JUDGE', 'jev')
        if backend_name not in ('local', 'ollama', 'jev', 'laya'):
            raise ValueError('Unsupported backend')
        backend_name = 'ollama' if backend_name == 'local' else backend_name
        if allow_external_state is None:
            consent = os.getenv('FAST_DECISIONS_ALLOW_EXTERNAL_STATE', 'false').strip().lower()
            if consent not in ('true', 'false', '1', '0', 'yes', 'no'):
                raise ValueError('Invalid external-state consent')
            allow_external_state = consent in ('true', '1', 'yes')
        if not isinstance(allow_external_state, bool):
            raise ValueError('Consent must be boolean')
        external = backend_name == 'jev' or bool(getattr(_backend, 'external', False))
        if external and not allow_external_state:
            return Selection(False, 'abstain', 'external_state_not_enabled', session, parent, decision_id,
                             backend=backend_name, remediation='Enable external state explicitly before using Jev.')
        if backend_name == 'jev' and _backend is None and not os.getenv('TYPESAFE_API_KEY'):
            return Selection(False, 'abstain', 'missing_api_key', session, parent, decision_id,
                             backend=backend_name, remediation='Set TYPESAFE_API_KEY in the process environment.')
        if model is None:
            model = (os.getenv('TYPESAFE_DEFAULT_MODEL') or DEFAULT_JEV_MODEL) if backend_name == 'jev' else (os.getenv('FAST_DECISIONS_LOCAL_MODEL') or 'qwen3:0.6b')
        if isinstance(timeout_ms, bool) or not isinstance(timeout_ms, int) or not 10 <= timeout_ms <= 500:
            raise ValueError('Deadline must be 10–500 ms')
        if not isinstance(model, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.:/-]{0,127}', model):
            raise ValueError('Invalid model name')
        scorer = _backend if _backend is not None else (
            JevBackend(model=model, timeout_ms=timeout_ms) if backend_name == 'jev' else
            LayaBackend(url=laya_url, timeout_ms=timeout_ms) if backend_name == 'laya' else
            OllamaBackend(model=model, url=ollama_url or os.getenv('FAST_DECISIONS_OLLAMA_URL', 'http://127.0.0.1:11434'), timeout_ms=timeout_ms))
        if getattr(scorer, 'external', False) and not allow_external_state:
            if _backend is None:
                await scorer.close()
            return Selection(False, 'abstain', 'external_state_not_enabled', session, parent, decision_id,
                             backend=backend_name, remediation='Use loopback Laya or explicitly permit external state.')
    except (ValueError, TypeError, OverflowError):
        return Selection(False, 'abstain', 'unsupported_request', session, parent, decision_id,
                         remediation='Check describe and select --help for bounded input, backend, consent and endpoint settings.')
    recorder = None
    duration = 0.0
    try:
        recorder = JsonlRecorder(events_dir if events_dir is not None else os.getenv('AFAST_EVENTS_DIR') or DEFAULT_EVENTS, session)
        emitter = Emitter(session, parent_session_id=parent, recorder=recorder, synthetic=bool(getattr(scorer, 'synthetic', False)))
        common = {'mode': 'advisory', 'backend': scorer.name, 'event_source': 'portable-smart-tool',
                  'engine': harness, 'allow_external_state': allow_external_state}
        await emitter.emit('requested', {**common, 'candidate_count': len(request.candidates),
            'candidates': [{'id': c.id, 'label': f'Candidate {i + 1}'} for i, c in enumerate(request.candidates)]}, decision_id=decision_id)
        started = time.perf_counter()
        try:
            async with asyncio.timeout(timeout_ms / 1000):
                response = await scorer.ask(request)
            duration = (time.perf_counter() - started) * 1000
            decision = response.action
            decision.validate({c.id for c in request.candidates} | {SLOW})
            if decision.synthetic or response.synthetic:
                raise ValueError('Unexpected model evidence')
            if backend_name == 'ollama':
                if decision.model != model or decision.probability_kind != PROBABILITY_KIND:
                    raise ValueError('Unexpected local model evidence')
            elif backend_name == 'laya':
                if (scorer.name != 'laya' or not isinstance(decision.model, str)
                    or not decision.model.strip() or decision.model == 'unknown'
                    or response.model != decision.model or decision.probability_kind != 'model_reported'):
                    raise ValueError('Unexpected Laya model evidence')
            elif (scorer.name != 'jev' or not isinstance(decision.model, str)
                  or not decision.model.strip() or decision.model == 'unknown'
                  or response.model != decision.model or decision.probability_kind != 'backend_reported'):
                raise ValueError('Unexpected Jev model evidence')
            probability = decision.probabilities[decision.choice]
            margin = probability - max((p for k, p in decision.probabilities.items() if k != decision.choice), default=0)
            await emitter.emit('scored', {**common, 'model': decision.model, 'choice': decision.choice,
                'probabilities': decision.probabilities, 'probability_kind': decision.probability_kind,
                'confidence_kind': decision.confidence_kind, 'option_set_hash': decision.option_set_hash,
                'probability_kind': decision.probability_kind,
                'input_tokens': response.input_tokens, 'output_tokens': response.output_tokens,
                'selected_probability': probability, 'margin': margin, 'duration_ms': duration,
                'latency_kind': 'decision_model_wall_time'}, decision_id=decision_id)
            selected = decision.choice != SLOW and probability >= .90 and margin >= .20
            reason = 'advisory_selected' if selected else ('model_abstained' if decision.choice == SLOW else 'selection_threshold')
            result = Selection(True, 'selected' if selected else 'abstain', reason, session, parent, decision_id,
                choice=decision.choice if selected else None, model=decision.model, backend=scorer.name,
                probabilities=dict(decision.probabilities), probability_kind=decision.probability_kind, duration_ms=duration,
                confidence_kind=decision.confidence_kind, option_set_hash=decision.option_set_hash,
                input_tokens=response.input_tokens, output_tokens=response.output_tokens)
        except asyncio.CancelledError:
            await emitter.emit('cancelled', {**common, 'reason_code': 'caller_cancelled'}, decision_id=decision_id)
            raise
        except Exception:
            duration = (time.perf_counter() - started) * 1000
            result = Selection(False, 'abstain', 'model_unavailable_or_invalid', session, parent, decision_id,
                backend=backend_name, duration_ms=duration,
                remediation=('Check Jev reachability, credentials and model; no alternate backend was used.' if backend_name == 'jev' else
                             'Start the Laya decide server and verify its health and queue latency.' if backend_name == 'laya' else
                             'Start Ollama, pull and warm the configured model; verify native token-log-probability support and input bounds.'))
        await emitter.emit('health', {**common, 'phase': 'advisory_result', 'status': result.status,
            'reason_code': result.reason_code, 'success': result.ok, 'duration_ms': duration}, decision_id=decision_id)
    except (OSError, ValueError):
        result = Selection(False, 'abstain', 'telemetry_unavailable', session, parent, decision_id,
                           remediation='Choose a writable --events directory outside the installed package.')
    finally:
        if _backend is None:
            await scorer.close()
        if recorder is not None:
            await asyncio.to_thread(recorder.close)
    if recorder is not None and (recorder.error or recorder.dropped):
        return Selection(False, 'abstain', 'telemetry_unavailable', session, parent, decision_id,
                         duration_ms=duration, remediation='Check events storage and retry after recording is healthy.')
    return result


async def search(payload, *, root=".", backend="jev", laya_url=None,
                 allow_external_state=False, timeout_ms=60000):
    from .jevgrep import JevgrepTool
    return await JevgrepTool(root=root, backend=backend, laya_url=laya_url,
                            allow_external_state=allow_external_state,
                            timeout_ms=timeout_ms).search(payload)


async def cua(payload, *, backend="jev", laya_url=None,
              allow_external_state=False, timeout_ms=3000, min_probability=.75):
    from .jev_cua import CuaSelector
    if not isinstance(payload, dict) or set(payload) != {"goal", "snapshot"}:
        raise ValueError("Expected goal and snapshot")
    selector = CuaSelector(backend=backend, laya_url=laya_url,
                           allow_external_state=allow_external_state,
                           timeout_ms=timeout_ms, min_probability=min_probability)
    try:
        return await selector.choose(payload["goal"], payload["snapshot"])
    finally:
        await selector.close()
