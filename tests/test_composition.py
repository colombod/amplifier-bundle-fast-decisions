"""Bundle-composition checks.

Two independent concerns live here:

1. ``YamlSourceTests`` parses bundle.md / behaviors / bundles YAML directly
   (no amplifier_foundation needed) and checks every module ``source:``
   points at the supported ``git+https://.../amplifier-bundle-fast-decisions@main#subdirectory=modules/<name>``
   form, with a matching ``modules/<name>/pyproject.toml`` declaring the
   right entry point. This always runs.

2. ``BundleLoadTests`` actually loads the bundles through
   amplifier_foundation.load_bundle() and inspects the compiled mount plan.
   It is skipped whenever amplifier_foundation is not installed (the
   offline CI lane). It requires network access to resolve the foundation
   include (git+https://github.com/microsoft/amplifier-foundation@main),
   which is acceptable in the upstream CI lane this test is meant for.
"""

from __future__ import annotations

import asyncio
import importlib.util
import re
import unittest
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]

UNSUPPORTED_PREFIX = "fast-decisions:modules/"

# Files that may declare a module `source:` pointing into modules/.
COMPOSITION_FILES = [
    ROOT / "bundle.md",
    ROOT / "behaviors" / "fast-decisions.yaml",
    ROOT / "behaviors" / "fast-decisions-shadow.yaml",
    ROOT / "bundles" / "shadow.yaml",
    ROOT / "bundles" / "active.yaml",
    ROOT / "bundles" / "active-routing.yaml",
]


def _load_frontmatter(path: Path) -> dict:
    """Parse a bundle file's YAML, whether it's frontmatter (bundle.md) or plain YAML."""
    text = path.read_text(encoding="utf-8")
    if path.suffix == ".md":
        match = re.match(r"^---\n(.*?)\n---\n?", text, re.DOTALL)
        if not match:
            raise ValueError(f"No YAML frontmatter found in {path}")
        text = match.group(1)
    return yaml.safe_load(text)


def _iter_module_sources(data: dict):
    """Yield (module_name, source) pairs for every module entry in a parsed bundle doc."""
    for section in ("tools", "hooks", "providers"):
        for entry in data.get(section, []) or []:
            if isinstance(entry, dict) and "source" in entry:
                yield entry.get("module"), entry["source"]
    session = data.get("session") or {}
    orchestrator = session.get("orchestrator") or {}
    if isinstance(orchestrator, dict) and "source" in orchestrator:
        yield orchestrator.get("module"), orchestrator["source"]
    context = session.get("context") or {}
    if isinstance(context, dict) and "source" in context:
        yield context.get("module"), context["source"]


class MakeFasterContextTests(unittest.TestCase):
    """The 'make my amplifier faster' trigger context file must exist and be
    referenced from behaviors/fast-decisions.yaml so a shadow-installed user
    (behavior-only, no bundles/active.yaml) still gets the trigger."""

    def test_context_file_exists(self):
        path = ROOT / "context" / "make-faster.md"
        self.assertTrue(path.is_file(), path)
        text = path.read_text(encoding="utf-8")
        self.assertIn("make my amplifier faster", text.lower())
        self.assertIn("make-amplifier-faster.yaml", text)

    def test_behavior_references_context_file(self):
        data = _load_frontmatter(ROOT / "behaviors" / "fast-decisions.yaml")
        includes = (data.get("context") or {}).get("include") or []
        self.assertIn("fast-decisions:context/make-faster.md", includes)

    def test_shadow_behavior_references_context_file(self):
        data = _load_frontmatter(ROOT / "behaviors" / "fast-decisions-shadow.yaml")
        includes = (data.get("context") or {}).get("include") or []
        self.assertIn("fast-decisions:context/make-faster.md", includes)


class OrchestratorPrimaryBehaviorOfflineTests(unittest.TestCase):
    """behaviors/fast-decisions.yaml is the primary form: it replaces the
    host orchestrator with loop-fast-decisions (routing-only by default)."""

    def test_behavior_declares_orchestrator(self):
        data = _load_frontmatter(ROOT / "behaviors" / "fast-decisions.yaml")
        orchestrator = data["session"]["orchestrator"]
        self.assertEqual(orchestrator["module"], "loop-fast-decisions")
        config = orchestrator["config"]
        self.assertEqual(config["mode"], "active")
        self.assertEqual(config["backend"], "jev")
        self.assertIs(config["allow_external_state"], True)
        self.assertEqual(config["model_routing"]["provider_match"], "anthropic")
        self.assertEqual(config["model_routing"]["start_policy"], "judge")
        self.assertEqual(config["model_routing"]["cheap_max_workspace_files"], 300)
        self.assertIs(config["read_shortcut"], False)
        self.assertEqual(config["effort_routing"]["by_tier"], {"cheap": "medium", "strong": None})
        # No explicit upstream block: the root's loop-streaming settings are
        # inherited through composition and forwarded (upstream_config).
        self.assertNotIn("upstream", config)
        self.assertTrue(config["observatory"]["enabled"])

    def test_behavior_orchestrator_config_is_a_valid_policy(self):
        from amplifier_fast_decisions.contracts import Policy

        data = _load_frontmatter(ROOT / "behaviors" / "fast-decisions.yaml")
        policy = Policy.from_config(data["session"]["orchestrator"]["config"])
        self.assertEqual(policy.mode, "active")
        self.assertEqual(policy.effort_routing["implement"], "high")

    def test_shadow_behavior_does_not_swap_orchestrator(self):
        data = _load_frontmatter(ROOT / "behaviors" / "fast-decisions-shadow.yaml")
        self.assertNotIn("session", data)


class YamlSourceTests(unittest.TestCase):
    """Always-on checks against the raw YAML; no amplifier_foundation needed."""

    def test_default_behavior_includes_enabled_jevgrep(self):
        behavior = _load_frontmatter(ROOT / 'behaviors/fast-decisions.yaml')
        self.assertIn({'bundle': 'fast-decisions:behaviors/jevgrep'}, behavior['includes'])
        retrieval = _load_frontmatter(ROOT / 'behaviors/jevgrep.yaml')
        tool = next(t for t in retrieval['tools'] if t['module'] == 'tool-jevgrep')
        self.assertTrue(tool['config']['allow_external_state'])

    def test_module_sources_use_supported_form_and_resolve(self):
        checked_any = False
        for path in COMPOSITION_FILES:
            data = _load_frontmatter(path)
            for module_name, source in _iter_module_sources(data):
                if UNSUPPORTED_PREFIX in source:
                    self.fail(
                        f"{path}: module {module_name!r} uses unsupported "
                        f"'{UNSUPPORTED_PREFIX}' source form: {source!r}"
                    )
                if "amplifier-bundle-fast-decisions" not in source:
                    # Not one of our own modules (e.g. foundation's loop-streaming); skip.
                    continue
                checked_any = True
                match = re.fullmatch(
                    r"git\+https://github\.com/michaeljabbour/amplifier-bundle-fast-decisions@"
                    r"(?:main|[0-9a-f]{40})#subdirectory=modules/([\w-]+)", source,
                )
                self.assertIsNotNone(match, f"{path}: unsupported module source {source!r}")
                mod_dir_name = match.group(1)
                module_pyproject = ROOT / "modules" / mod_dir_name / "pyproject.toml"
                self.assertTrue(
                    module_pyproject.is_file(),
                    f"{path}: module {module_name!r} references "
                    f"modules/{mod_dir_name}/pyproject.toml, which does not exist",
                )
                entry_point_pattern = re.compile(
                    r'\[project\.entry-points\."amplifier\.modules"\]\s*\n'
                    rf"{re.escape(mod_dir_name)}\s*="
                )
                self.assertRegex(
                    module_pyproject.read_text(encoding="utf-8"),
                    entry_point_pattern,
                    f"modules/{mod_dir_name}/pyproject.toml does not declare the "
                    f"'{mod_dir_name}' entry point under [project.entry-points.\"amplifier.modules\"]",
                )
        self.assertTrue(
            checked_any, "Expected to find at least one fast-decisions module source"
        )


class ActiveBundleOfflineTests(unittest.TestCase):
    """Offline (no amplifier_foundation) checks on bundles/active.yaml's raw
    YAML -- runs even when BundleLoadTests below is skipped for lack of
    network access to resolve the foundation include."""

    def test_active_yaml_parses_and_carries_incumbent_config(self):
        data = _load_frontmatter(ROOT / "bundles" / "active.yaml")
        self.assertEqual(data["bundle"]["name"], "fast-decisions-active")
        orchestrator = data["session"]["orchestrator"]
        self.assertEqual(orchestrator["module"], "loop-fast-decisions")
        config = orchestrator["config"]
        self.assertEqual(config["mode"], "active")
        self.assertEqual(config["backend"], "ollama")
        self.assertEqual(config["model"], "qwen3:0.6b")
        self.assertEqual(config["timeout_ms"], 500)
        self.assertIs(config["allow_external_state"], False)
        self.assertEqual(
            config["effort_routing"],
            {"explore": "low", "max_explore_requests": 6, "escalate_after_provider_errors": 1},
        )
        self.assertNotIn("model_routing", config)
        self.assertEqual(len(data["includes"]), 1)
        self.assertRegex(data["includes"][0]["bundle"],
            r"^git\+https://github.com/michaeljabbour/amplifier-bundle-fast-decisions@[0-9a-f]{40}$")


class ActiveRoutingBundleOfflineTests(unittest.TestCase):
    """Offline (no amplifier_foundation) checks on bundles/active-routing.yaml's
    raw YAML, plus a Policy.from_config validation pass against contracts.py --
    both run even when BundleLoadTests below is skipped for lack of network
    access to resolve the foundation include."""

    def test_active_routing_yaml_parses_and_carries_routing_config(self):
        data = _load_frontmatter(ROOT / "bundles" / "active-routing.yaml")
        self.assertEqual(data["bundle"]["name"], "fast-decisions-active-routing")
        orchestrator = data["session"]["orchestrator"]
        self.assertEqual(orchestrator["module"], "loop-fast-decisions")
        config = orchestrator["config"]
        self.assertEqual(config["mode"], "active")
        self.assertEqual(config["backend"], "ollama")
        self.assertEqual(config["model"], "qwen3:0.6b")
        self.assertEqual(config["timeout_ms"], 500)
        self.assertIs(config["allow_external_state"], False)
        self.assertEqual(
            config["effort_routing"],
            {
                "orient": "medium",
                "explore": "low",
                "implement": "high",
                "max_explore_requests": 6,
                "escalate_after_provider_errors": 1,
            },
        )
        self.assertEqual(
            config["model_routing"],
            {
                "start_model": "claude-sonnet-5",
                "max_requests_before_escalation": 6,
                "escalate_on_test_failure": True,
                "escalate_on_provider_error": True,
            },
        )
        self.assertEqual(data["includes"], [
            {"bundle": "git+https://github.com/microsoft/amplifier-foundation@main"},
            {"bundle": "fast-decisions:behaviors/fast-decisions-shadow"},
        ])

    def test_active_routing_config_validates_against_policy(self):
        """The orchestrator config must be a valid Policy -- this catches a
        typo'd or unsupported key at test time instead of at mount time."""
        from amplifier_fast_decisions.contracts import Policy

        data = _load_frontmatter(ROOT / "bundles" / "active-routing.yaml")
        config = data["session"]["orchestrator"]["config"]
        # Policy.from_config only accepts fields it knows about (plus
        # upstream/allowed_tools handling done by the orchestrator itself);
        # drop the keys that belong to the orchestrator wrapper, not Policy.
        policy_config = {k: v for k, v in config.items() if k != "upstream"}
        policy = Policy.from_config(policy_config)
        self.assertEqual(policy.mode, "active")
        self.assertEqual(policy.effort_routing["orient"], "medium")
        self.assertEqual(policy.effort_routing["implement"], "high")
        self.assertEqual(policy.model_routing["start_model"], "claude-sonnet-5")


def _amplifier_foundation_importable() -> bool:
    """True only if amplifier_foundation actually imports (not merely findable).

    A stale/partial editable install (present on sys.path but missing a
    transitive dependency such as amplifier_core) must skip, not error.
    """
    if importlib.util.find_spec("amplifier_foundation") is None:
        return False
    try:
        importlib.import_module("amplifier_foundation")
    except ImportError:
        return False
    return True


@unittest.skipUnless(
    _amplifier_foundation_importable(),
    "amplifier_foundation not installed (or not importable)",
)
class BundleLoadTests(unittest.TestCase):
    """Loads real bundles through amplifier_foundation and inspects the mount plan.

    Requires network access to resolve git+https://.../amplifier-foundation@main;
    intended for the upstream CI lane.
    """

    @staticmethod
    def _load(relative_path: str):
        from amplifier_foundation import load_bundle

        uri = "file://" + str((ROOT / relative_path).resolve())
        return asyncio.run(load_bundle(uri))

    def test_root_bundle_composes_with_foundation(self):
        bundle = self._load("bundle.md")
        self.assertEqual(bundle.name, "fast-decisions")
        mount_plan = bundle.to_mount_plan()

        orchestrator = mount_plan["session"]["orchestrator"]
        self.assertEqual(
            orchestrator["module"],
            "loop-fast-decisions",
            "root bundle.md must replace foundation's orchestrator",
        )
        # Foundation's own loop-streaming setting survives the swap (it is
        # forwarded to the wrapped loop by orchestrator.upstream_config).
        from amplifier_fast_decisions.orchestrator import upstream_config

        self.assertIs(upstream_config(orchestrator["config"]).get("extended_thinking"), True)

        tool_modules = {t["module"] for t in mount_plan.get("tools", [])}
        hook_modules = {h["module"] for h in mount_plan.get("hooks", [])}
        self.assertIn("tool-fast-workspace", tool_modules)
        self.assertIn("tool-jevgrep", tool_modules)
        retrieval = next(t for t in mount_plan["tools"] if t["module"] == "tool-jevgrep")
        self.assertTrue(retrieval["config"]["allow_external_state"])
        self.assertIn("hooks-fast-decisions", hook_modules)

    def _assert_decision_bundle(
        self,
        relative_path: str,
        expected_name: str,
        expected_mode: str,
        expected_allow_external: bool,
    ):
        bundle = self._load(relative_path)
        self.assertEqual(bundle.name, expected_name)
        mount_plan = bundle.to_mount_plan()

        orchestrator = mount_plan["session"]["orchestrator"]
        self.assertEqual(orchestrator["module"], "loop-fast-decisions")

        config = orchestrator["config"]
        self.assertEqual(config["mode"], expected_mode)
        self.assertIs(config["allow_external_state"], expected_allow_external)
        self.assertIn("upstream", config)

        self.assertRegex(
            orchestrator["source"],
            r"^git\+https://github\.com/michaeljabbour/amplifier-bundle-fast-decisions@"
            r"(?:main|[0-9a-f]{40})#subdirectory=modules/loop-fast-decisions$",
        )

        # Inherited from root bundle.md via the includes chain.
        tool_modules = {t["module"] for t in mount_plan.get("tools", [])}
        hook_modules = {h["module"] for h in mount_plan.get("hooks", [])}
        self.assertIn("tool-fast-workspace", tool_modules)
        self.assertIn("hooks-fast-decisions", hook_modules)

    def test_shadow_bundle_is_deprecated_forwarding_alias(self):
        """bundles/shadow.yaml no longer swaps session.orchestrator (P3): shadow
        measurement now lives on the hook and composes onto any orchestrator.
        This bundle still resolves and still names/configures the shadow
        rung, but the orchestrator stays loop-streaming (inherited via
        bundle.md), and its description opens with DEPRECATED."""
        bundle = self._load("bundles/shadow.yaml")
        self.assertEqual(bundle.name, "fast-decisions-shadow")
        self.assertTrue(bundle.description.strip().startswith("DEPRECATED"))
        mount_plan = bundle.to_mount_plan()

        orchestrator = mount_plan["session"]["orchestrator"]
        self.assertEqual(
            orchestrator["module"],
            "loop-streaming",
            "bundles/shadow.yaml must no longer swap the orchestrator",
        )

        hook_modules = {h["module"]: h for h in mount_plan.get("hooks", [])}
        self.assertIn("hooks-fast-decisions", hook_modules)
        self.assertEqual(
            hook_modules["hooks-fast-decisions"]["config"]["mode"], "shadow"
        )

        tool_modules = {t["module"] for t in mount_plan.get("tools", [])}
        self.assertIn("tool-fast-workspace", tool_modules)

    def test_active_bundle(self):
        # Screen-validated 2026-09-20 incumbent config: ollama/qwen3:0.6b,
        # allow_external_state False (no TypeSafe key required to install).
        self._assert_decision_bundle(
            "bundles/active.yaml",
            expected_name="fast-decisions-active",
            expected_mode="active",
            expected_allow_external=False,
        )
        bundle = self._load("bundles/active.yaml")
        config = bundle.to_mount_plan()["session"]["orchestrator"]["config"]
        self.assertEqual(config["backend"], "ollama")
        self.assertEqual(config["model"], "qwen3:0.6b")
        self.assertEqual(config["timeout_ms"], 500)
        self.assertEqual(
            config["effort_routing"],
            {"explore": "low", "max_explore_requests": 6, "escalate_after_provider_errors": 1},
        )
        self.assertNotIn("model_routing", config)


def _configure(tmp_path: Path, mode: str, output_name: str) -> Path:
    from types import SimpleNamespace

    from amplifier_fast_decisions.cli import configure

    output = tmp_path / output_name
    args = SimpleNamespace(
        bundle_root=str(ROOT),
        workspace=str(tmp_path),
        mode=mode,
        allow_external_state=True,
        events=str(tmp_path / "events"),
        timeout_ms=750,
        output=str(output),
    )
    configure(args)
    return output


class ConfigureFrontmatterTests(unittest.TestCase):
    """Plain-lane (no amplifier_foundation) checks on the generated profile shape."""

    def test_shadow_profile_has_no_session_block(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            output = _configure(Path(tmp), "shadow", "local-shadow.md")
            data = _load_frontmatter(output)
            self.assertNotIn("session", data)
            hook_modules = {h["module"]: h for h in data.get("hooks", [])}
            self.assertIn("hooks-fast-decisions", hook_modules)
            self.assertIn("source", hook_modules["hooks-fast-decisions"])
            self.assertEqual(
                hook_modules["hooks-fast-decisions"]["config"]["mode"], "shadow"
            )

    def test_active_profile_has_orchestrator_source(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            output = _configure(Path(tmp), "active", "local-active.md")
            data = _load_frontmatter(output)
            orchestrator = data["session"]["orchestrator"]
            self.assertIn("source", orchestrator)
            self.assertEqual(orchestrator["module"], "loop-fast-decisions")
            self.assertEqual(orchestrator["config"]["mode"], "active")


@unittest.skipUnless(
    _amplifier_foundation_importable(),
    "amplifier_foundation not installed (or not importable)",
)
class ConfigureProfileCompositionTests(unittest.TestCase):
    """Generate shadow/active profiles via configure() and compose them for real."""

    @staticmethod
    def _load(path: Path):
        from amplifier_foundation import load_bundle

        return asyncio.run(load_bundle("file://" + str(path.resolve())))

    def test_generated_shadow_profile_keeps_loop_streaming(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            output = _configure(Path(tmp), "shadow", "local-shadow.md")
            bundle = self._load(output)
            mount_plan = bundle.to_mount_plan()

            orchestrator = mount_plan["session"]["orchestrator"]
            self.assertEqual(
                orchestrator["module"],
                "loop-streaming",
                "generated shadow profile must not swap the orchestrator",
            )

            hook_modules = {h["module"]: h for h in mount_plan.get("hooks", [])}
            self.assertIn("hooks-fast-decisions", hook_modules)
            hook_config = hook_modules["hooks-fast-decisions"]["config"]
            self.assertEqual(hook_config["backend"], "jev")
            self.assertIs(hook_config["allow_external_state"], True)

    def test_generated_active_profile_swaps_orchestrator(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            output = _configure(Path(tmp), "active", "local-active.md")
            bundle = self._load(output)
            mount_plan = bundle.to_mount_plan()

            orchestrator = mount_plan["session"]["orchestrator"]
            self.assertEqual(orchestrator["module"], "loop-fast-decisions")
            self.assertIn("source", orchestrator)
            self.assertEqual(orchestrator["config"]["mode"], "active")


if __name__ == "__main__":
    unittest.main()
