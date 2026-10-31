"""Adapter: lean-coder Agent -> the gym engine's agent interface (D13b).

The engine needs `.messages` ([0] = system prompt) and `.send(prompt) -> final_text`.
This wraps agent_fixture.build_agent (fresh lc.Config, paid gate, provider plumbing) so
every existing safety gate still applies. Prompt-cache note: the system prompt embeds
cwd, so the engine runs every rerun at one FIXED workspace path (gym/stages.py)."""
from __future__ import annotations

from pathlib import Path


class LeanCoderAgent:
    def __init__(self, ws: Path, *, host: str, model: str, think: bool = False,
                 provider: str | None = None, isolate=None, **kw):
        import agent_fixture as af
        self._af = af
        self.spec = dict(host=host, model=model, think=think, provider=provider)  # worker stage
        self.agent = af.build_agent(host, model, cwd=Path(ws), think=think, provider=provider, **kw)
        self.spec["provider"] = self.agent.cfg.provider      # resolved (EVAL_PROVIDER etc.)
        if isolate is not None:                              # gym home redirect must hold
            from gym.home import leaks
            from gym.home import provider_globals_for
            bad = leaks(af.lc, isolate)
            p = self.agent.cfg.provider
            if p and p != "ollama" and provider_globals_for(af.lc, p) is None:
                bad = bad + [f"{p}:unverifiable"]
            if bad or Path(af.lc.CONFIG_DIR) != Path(isolate):
                raise RuntimeError(f"isolation leak: CONFIG_DIR={af.lc.CONFIG_DIR} providers={bad}")

    @property
    def messages(self) -> list:
        return self.agent.messages

    def send(self, prompt: str) -> str:
        self.agent.run_turn(prompt)
        return self._af.last_assistant_text(self.agent)


def factory(**kw):
    """-> make_agent(ws) for gym.stages.run_task."""
    return lambda ws: LeanCoderAgent(ws, **kw)
