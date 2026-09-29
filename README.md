# Fast Decisions

Fast Decisions makes Amplifier faster by asking one quick question at the start of every request: *is this easy
or hard?* Easy requests go to a faster, cheaper model (Claude Sonnet 5). Hard ones, and all work inside large
projects, stay on your usual model. The decision is made once per request, so the AI service's memory of the
conversation is never thrown away mid-request.

Experimental, MIT licensed. Not an official Microsoft or TypeSafe release, and no number here is a guarantee.
Approvals, permissions and tool execution are unchanged: Fast Decisions wraps Amplifier's standard loop rather
than replacing it.

**Default judge: Jev.** Routing and opt-in computer-use selection use Jev 1.13.0;
source relevance search uses upstream Jevgrep. Configure `TYPESAFE_API_KEY` privately.
The bundle behaviors explicitly permit bounded external state; standalone calls
require `--allow-external-state`. Laya remains an explicit experimental backend.
AnyJev remains disabled in the normal path.

**Quality check, September 29:** base Laya scored 35/60 versus Jev's 56/60
on constructed decision cases. The specialized Laya checkpoint scored 34/60;
on 30 fresh cases, base/specialized/Jev scored 19/22/29. These results do not
support broad automatic Laya routing or inherit the savings below. Domain tuning
and separate held-out validation are needed before promotion. See the
[latency and quality study](docs/evidence/2026-09-29-laya-hosted/README.md)
and its [interactive results](docs/evidence/2026-09-29-laya-hosted/index.html).

## What it does (measured)

| Kind of work | Time | Cost | Quality |
|---|---|---|---|
| Everyday coding tasks, expensive default model (Claude Fable 5.1) | **0.52×** | **0.49×** | Every scored run passed (36 of 36 vs. 36 of 36) |
| Everyday coding tasks, Claude Opus 5.5 as default | **0.80×** | 0.97× | Every scored run passed (36 of 36 vs. 36 of 36) |
| Multi-turn sessions (4 requests), Fable 5.1 default · screen | 0.45× | 0.66–0.73× | 9 of 9 vs. 9 of 9 |
| Multi-turn sessions (4 requests), Opus 5.5 default · screen | 0.89× | **1.34–1.41×** (costs more) | 9 of 9 vs. 9 of 9 |
| Real bug fixes in large projects (SWE-bench Verified) | 1.00× | 0.98× | 13 of 20 fixed vs. 14 of 20 (same setup as standard) |

Everyday rows: the historical Jev configuration (one decision per request) on a fresh split of 12 tasks never run
before (`holdout2`, 4 of them longer multi-file tasks), 3 runs each, one request per fresh session, against standard
Amplifier with the same default model. The criteria were written down and pushed 27 s before the first run, and the
setups ran in a shuffled order per repetition. Both results are **confirmed** under the study protocol: Fable 95%
interval 0.42–0.65, faster on 11 of 12 tasks (sign test p = 0.006); Opus 95% interval 0.67–0.92, faster on 10 of 12
(p = 0.04). These replace the earlier unseen-task run (Fable 0.42×, Opus 1.01× "no measurable gain"), which reused
its tasks and ran in a fixed order. Multi-turn rows: a new suite, one session with 4 requests, 3 scenarios × 3 runs,
tuning tasks only, so a screen, not a confirmation; the cost range is two estimators (pipeline and per-task). Ratios
are geometric means of per-task ratios. Bug-fix setups ran at the same time.

**Who benefits.** Easy requests go to Claude Sonnet 5, so the gain depends on your default model. With an expensive
default it is large, for single requests and multi-turn sessions alike. With Claude Opus 5.5 there is a confirmed
speed gain of about 20% on single requests at about equal cost. In multi-turn sessions on Opus it is ~10% faster but
costs 1.34–1.41× more: once a session's conversation is cached, Opus 5.5 reads it at $0.20 per million tokens, below
Sonnet 5's $0.30, and each switch to Sonnet forces a fresh cache write. A cache- and price-aware turn planner for this
is being developed on a follow-up branch. With Opus, route if single requests and helper sessions dominate your use
(on the author's machine, 74% of provider calls in the last 14 days were first requests of helper sessions, 12% later
requests of main sessions); turn model routing off if long main-session conversations dominate and cost matters more
than speed. On small tasks much of the gain is the faster model itself: against standard Amplifier switched to
Sonnet, Fast Decisions took 1.04× the time and 1.25× the cost (Fable default) and 0.87× the time at 1.01× the cost
(Opus default). The dispatcher's job is keeping hard requests and large projects on your usual model.

**Not recommended: easy requests on Claude Haiku 4.5.** A cost preset that sent easy requests to Haiku (with extra
guidance and the to-do tool hidden) cost 0.45× (Opus default) and 0.30× (Fable default) on `holdout2`, but Haiku got
`repair_roman_to_int` wrong in 4 of 6 runs where standard Amplifier got it right (its check accepted out-of-order
numerals). That is a critical failure under the protocol, so the preset is disqualified; on the Opus default it was
also 1.16× slower. In tuning on the Opus default, Haiku made more round trips (planning and checklist tool calls) and
was ~1.2× slower despite faster calls; the extra guidance cut its round trips, but not enough. Lowering the easy
tier's thinking level, or staying on Opus at a low thinking level, did not beat Sonnet (tuning screens).

Full results: [report](docs/report/fast-decisions-report.html) (plain language) ·
[RESULTS-2026-09-24.md](docs/RESULTS-2026-09-24.md) · [study protocol](evals/STUDY-DESIGN.md) §18 ·
[holdout2 verification](docs/evidence/2026-09-25/holdout2/VERIFICATION.md) ·
evidence with checksums: [2026-09-24](docs/evidence/2026-09-24/), [2026-09-25](docs/evidence/2026-09-25/).

## Install

One command. It applies to the Amplifier command line, the Amplifier terminal app and Studio, because all three
read `~/.amplifier/settings.yaml`, and to every helper session they start:

```bash
amplifier bundle add --app "git+https://github.com/michaeljabbour/amplifier-bundle-fast-decisions@main#subdirectory=behaviors/fast-decisions.yaml"
```

- **Decision-maker:** configure `TYPESAFE_API_KEY` for Jev 1.13.0.
  If it is unavailable, routing uses its existing rule fallback; standalone tools report failure.
  Jev remains an explicit opt-in backend requiring a TypeSafe key and external-state consent.
- **Upgrading from an earlier version:** run `amplifier update` once, and remove any older fast-decisions entry
  (`amplifier bundle remove --app <old-uri>`). Keep this entry last in your app list: later entries win.
- **Check it:** run a prompt, then
  `uvx --from git+https://github.com/michaeljabbour/amplifier-bundle-fast-decisions afast savings`.
  A dashboard also opens at the start of a session (`afast serve --stop` closes it).
- **Your choice wins:** picking a model yourself during a session keeps every request on that model (verified in
  the terminal app; Studio uses the same engine).
- **Turn it off:** remove the app entry. To keep it but stay fully local, see *Who decides* below.

## How it works

1. **One decision per request.** Before the first model call, the decision-maker judges the request easy or hard.
   That picks the model and thinking level for the whole request; nothing switches halfway (only a provider error
   on the faster model moves the rest of the request to your usual model; the failed call itself is not retried).
2. **Large projects always get your usual model.** When the session's folder holds more than 300 files (not counting
   .git, dependency, virtualenv and build folders), the request runs on your usual model and thinking level, whatever
   the decision-maker says. On real bug fixes, starting on the cheaper model
   lost fixes (19 of 30 vs. 23 of 30) and switching up later did not recover them.
3. **Everything is recorded.** Each decision, who made it, and each model call's tokens and cost go to a local
   log (no prompts or file contents) that the dashboard and `afast savings` read.

Design detail: [docs/ORCHESTRATOR-PRIMARY.md](docs/ORCHESTRATOR-PRIMARY.md).

## Who decides

Measured on 90 real issues rated by human experts (SWE-bench Verified): how often each decision-maker ranks an issue
rated an hour or more of work above one rated under 15 minutes (AUC), and how long it takes. 0.5 is a coin flip. Issues in
between were not included, so everyday accuracy will be lower. Re-run on 2026-09-25; figures reproduced.

| Decision-maker | Picks the harder issue (AUC) | Time per decision | Data leaves your machine |
|---|---|---|---|
| **Jev** (hosted by TypeSafe) · historical baseline | 0.83 | ~0.14–0.16 s | The first 2,500 characters of your latest message (for helper sessions, of the helper's instructions) |
| Built-in rule · routing fallback on unavailable judge | 0.60 | instant | No |
| Large local Qwen model (Ollama `qwen:latest`, 27.4B parameters, 29 GB) | 0.85 | ~2.2–2.5 s | No |
| Qwen3 8B on your Mac (Ollama `qwen3:8b`) | 0.72 | ~0.36 s | No |
| Qwen3.8 27B on a shared hosted server (RunPod) | 0.85 | median ~0.6 s; 18–25% of calls took 5–24 s | To your server |

Switch in `~/.amplifier/settings.yaml` (no bundle edit):

```yaml
overrides:
  loop-fast-decisions:
    config:
      backend: none                 # built-in rule only; nothing leaves the machine
      allow_external_state: false
      # or a local model:  backend: ollama, model: "qwen3:8b", timeout_ms: 3000
```

The built-in rule alone (no key) measured 0.55× / 0.38× on the 12 tuning tasks and 0.61× / 0.40× on the 8 unseen
tasks (Fable default); its confirmation run met 5 of 6 criteria (p = 0.07). Jev judges difficulty better; the rule
was cheaper on a set of all-easy tasks because it kept fewer requests on the usual model. Setup details: [docs/MODEL-SETUP.md](docs/MODEL-SETUP.md) · data paths:
[docs/PRIVACY.md](docs/PRIVACY.md).

## Also included

- **Code discovery:** the default `jevgrep` tool uses upstream Jevgrep with bounded source sharing.
  It honors ignore rules and workspace exclusions. This local implementation is distinct from upstream
  Jevgrep, which remains available with `backend: jev` and external-state consent ([details](docs/JEVGREP.md)).
  See the [matched Jev, Laya and retrieval measurements](docs/evidence/2026-09-28-decisions/REPORT.md) for task-level benefits and regressions.
- **Experimental computer use:** opt-in `fast-decisions:behaviors/jev-cua` selects an
  operation and observed UI control in one batched Jev request. A bounded host driver can
  continue between clicks without generative calls; native approvals, fresh targets,
  and completion verification remain host-owned. Includes a trycua click/wait adapter.
  See [setup, live evidence and current limits](docs/JEV-CUA.md).
- **Paused AnyJev research adapter:** explicitly opt-in only; optional fixed-question L0 evaluation and fitted L1/L2
  routing, with exact model/question checks. Added as an alternative to evaluate; it has not
  been selected by the normal default ([assessment and setup](docs/ANYJEV.md)).
- **Savings estimate:** `afast savings [--since 7d] [--json]` and the dashboard's savings panel price and time each
  cheaper-model request as if your usual model had done it, assuming your usual model would already have the
  conversation cached after the first request and charging the cache rebuild when a hard request follows an easy
  one. Estimates, labeled as such; a negative number means routing cost more.
- **Waste guards (on by default):** stop repeated identical calls and repeated identical failures, run
  `sleep && check` polls in place, and replace duplicate outputs with a pointer; every firing leaves a receipt in
  the Efficiency ledger (`afast efficiency`). `afast hooks install claude-code` applies the same guards in Claude
  Code ([docs/WASTE-GUARDS.md](docs/WASTE-GUARDS.md)). Measured target:
  [docs/evidence/2026-09-25/waste-census](docs/evidence/2026-09-25/waste-census/README.md).
- **Quality grader:** `afast rubric requests.jsonl` scores answers against weighted yes/no questions using Jev.
- **Watch-only mode:** `behaviors/fast-decisions-shadow.yaml` records what would have been decided without
  changing anything.
- **Hosts that own their loop (Amplifier Unified):** `behaviors/fast-decisions-registry.yaml` does the same
  routing, prepared actions and waste guards without replacing the orchestrator, so it runs under Unified's
  `loop-live` as well as `loop-streaming` ([docs/REGISTRY-MODE.md](docs/REGISTRY-MODE.md)).
- **Other assistants:** Claude Code, Codex and OpenCode can call the same decision service as a tool; it suggests,
  it doesn't change how they run ([docs/SMART-TOOL.md](docs/SMART-TOOL.md)).

## Limits

- Tested on one Apple-silicon Mac with the Anthropic provider; routing only sends the faster model to Anthropic
  providers. Other providers, models and machines may differ.
- The routing checks are confirmed end to end in the Amplifier command line, `amplifier-runtime serve` (the engine
  Studio uses) and the terminal app. Studio's window itself was not driven.
- A model set before a session starts (in settings or with `--model`) looks the same as your default, so it can
  still be routed; a model picked during the session is always respected.
- **Single requests only are confirmed.** The confirmed everyday results come from one request per fresh session.
  Multi-turn sessions are measured on tuning tasks only (3 scenarios); on the Opus default they cost more (see above).
- **Longer everyday tasks are only partly covered.** 4 of the 12 `holdout2` tasks are longer multi-file tasks; tasks
  with many more model calls per request are not in the suite.
- **Shared cache.** Runs share the provider's prompt cache across setups, so order could bias time and cost. On
  `holdout2` the order was shuffled per repetition and the per-repetition ratios show no trend with position.
- Ten bug-fix issues and twelve unseen everyday tasks are small samples. See
  [docs/COMPATIBILITY.md](docs/COMPATIBILITY.md) before relying on this in production.

## Development

```bash
uv pip install -e '.[test]'
python3 -m unittest discover -s tests -v     # test suite
python3 -m amplifier_fast_decisions doctor   # environment check
```

Evaluations: [evals/](evals/README.md) · metrics: [docs/BENCH.md](docs/BENCH.md) · events:
[docs/EVENTS.md](docs/EVENTS.md) · all settings: [docs/CONFIGURATION.md](docs/CONFIGURATION.md) ·
per-delegation routing (opt-in, off by default):
[docs/DELEGATION-ROUTING.md](docs/DELEGATION-ROUTING.md) · earlier studies
of the previous design: [docs/EVIDENCE.md](docs/EVIDENCE.md) · [CONTRIBUTING.md](CONTRIBUTING.md) ·
[SECURITY.md](SECURITY.md).

## License

MIT. See [LICENSE](LICENSE).
