"""Test-only local browser host. Never controls the user's desktop or arbitrary URLs.

Uses a real Playwright browser behind TryCuaHost's left_click protocol. This is
not evidence of an installed trycua VM. Native tool hooks remain in charge;
within an approved bounded run, only this fixture's visible buttons are allowed.
"""
import json
from pathlib import Path

from amplifier_core.models import HookResult, ToolResult
from amplifier_fast_decisions.cua_host import TryCuaHost
from amplifier_fast_decisions.jev_cua import CuaSelector, fresh, run

__amplifier_module_type__ = "tool"


class Browser:
    name = "fixture_browser"
    description = "Observe, click, verify, or run Jev on a disposable public report website. No arbitrary URLs or code."
    input_schema = {
        "type": "object", "required": ["action"], "additionalProperties": False,
        "properties": {
            "action": {"type": "string", "enum": ["observe", "click", "execute_proposal", "verify", "fast_run"]},
            "target": {"type": "string"}, "proposal": {"type": "object"},
        },
    }

    def __init__(self, config):
        self.root = Path(config["output"]).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.config = config
        self.page = None
        self.selector = CuaSelector(allow_external_state=True, timeout_ms=config.get("judge_timeout_ms", 3000))
        self.revision = 0
        self.host = TryCuaHost(self, observe_controls=self.observe_controls,
                               approve=self.approve, verify=self.verify)

    def record(self, event, **data):
        with (self.root / "browser-events.jsonl").open("a") as stream:
            stream.write(json.dumps({"event": event, **data}) + "\n")

    async def start(self):
        if self.page is not None:
            return
        from playwright.async_api import async_playwright
        self.playwright = await async_playwright().start()
        self.browser = await self.playwright.chromium.launch(headless=True)
        self.page = await self.browser.new_page(viewport={"width": 1000, "height": 700})
        await self.page.goto(Path(__file__).with_name("workflow.html").as_uri())

    async def observe_controls(self):
        await self.start()
        state = await self.page.evaluate("""() => ({step:document.body.dataset.step,
            text:document.querySelector('main').innerText,
            buttons:[...document.querySelectorAll('button')].map(b=>{const r=b.getBoundingClientRect();
                return {id:b.id,label:b.innerText,x:Math.round(r.x+r.width/2),y:Math.round(r.y+r.height/2)}})})""")
        elements = [{"id": b["id"], "label": b["label"], "role": "button", "operations": ["CLICK"]}
                    for b in state["buttons"]]
        return ({"surface_id": "public-report-fixture", "revision": state["step"] + str(self.revision),
                 "text": state["text"], "elements": elements},
                {b["id"]: (b["x"], b["y"]) for b in state["buttons"]})

    async def approve(self, action):
        observed, _ = await self.observe_controls()
        approved = (not self.config.get("deny_actions") and action.get("operation") == "CLICK"
                    and action.get("target") in {el["id"] for el in observed["elements"]})
        self.record("action_approval", action=action, approved=bool(approved))
        if self.config.get("approval_stales"):
            self.revision += 1
        return bool(approved)

    async def left_click(self, *, x, y):
        if not self.config.get("freeze_clicks"):
            await self.page.mouse.click(x, y)
        self.record("native_click", x=x, y=y)
        await self.checkpoint()

    async def verify(self, *_args):
        await self.start()
        return not self.config.get("verify_fail") and await self.page.locator("#receipt").count() == 1 and await self.page.locator("#receipt").inner_text() == "REPORT: LAYA-WEEKLY-42"

    async def checkpoint(self):
        state = await self.page.evaluate("() => ({step:document.body.dataset.step,history:window.historyLog, text:document.querySelector('main').innerText})")
        state["verified"] = await self.verify()
        (self.root / "browser-state.json").write_text(json.dumps(state, indent=2))
        await self.page.screenshot(path=str(self.root / "browser.png"))
        return state

    async def execute(self, input, **_kwargs):
        await self.start()
        action = input["action"]
        self.record("tool_execute", action=action)
        if action == "observe":
            output = await self.host.observe()
        elif action == "verify":
            output = await self.checkpoint()
        elif action == "fast_run":
            async def record(receipt):
                self.record("cua_step", **receipt)
            output = await run(self.selector, "Open the weekly Laya report. Complete only when REPORT: LAYA-WEEKLY-42 is visible.",
                               self.host, max_steps=6, record=record)
            (self.root / "driver-result.json").write_text(json.dumps(output, indent=2))
        elif action == "execute_proposal":
            proposal = input["proposal"]
            observed = await self.host.observe()
            accepted = proposal.get("status") == "proposal" and fresh(proposal, observed)
            if accepted:
                accepted = await self.approve(proposal["action"])
            output = {"executed": bool(accepted and await self.host.execute(proposal["action"], observed))}
        elif action == "click":
            observed = await self.host.observe()
            step = {"operation": "CLICK", "target": input["target"]}
            accepted = await self.approve(step)
            output = {"executed": bool(accepted and await self.host.execute(step, observed))}
        else:
            raise ValueError("Unsupported fixture action")
        await self.checkpoint()
        return ToolResult(success=True, output=output)

    async def close(self):
        await self.selector.close()
        if self.page is not None:
            await self.checkpoint()
            await self.browser.close()
            await self.playwright.stop()


async def mount(coordinator, config):
    tool = Browser(config)
    async def approval(event, data):
        name = data.get("tool_name")
        denied = bool(config.get("deny_selector") and name == "jev_cua")
        tool.record("native_tool_pre", tool_name=name, denied=denied)
        return HookResult(action="deny" if denied else "continue", reason="Fixture approval test" if denied else None)
    remove = coordinator.hooks.register("tool:pre", approval, priority=0, name="forge-cua-approval")
    await coordinator.mount("tools", tool, name=tool.name)
    async def cleanup():
        remove()
        await tool.close()
    return cleanup
