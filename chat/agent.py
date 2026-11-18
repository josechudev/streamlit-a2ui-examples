"""A Google ADK agent that answers with A2UI 1.0 messages (basic catalog).

The agent runs on its own event-loop thread. It never touches Streamlit: it
only appends to plain Python lists, which the app's fragment drains.
"""

from __future__ import annotations

import asyncio
import json
import os
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from a2ui.core.basic_catalog import BasicCatalog
from a2ui.parser.messages import to_message_dicts
from a2ui.parser.response_part import A2uiPart, ErrorPart, TextPart
from a2ui.processor.catalog_config import CatalogConfig
from a2ui.processor.generator import A2uiGenerator
from a2ui.utils.renderer_capabilities import read_renderer_capabilities
from dotenv import load_dotenv
from google.adk.agents import LlmAgent
from google.adk.agents.run_config import RunConfig, StreamingMode
from google.adk.runners import InMemoryRunner
from google.genai import types

# The key lives in .env at the repo root (gitignored); it is never printed.
load_dotenv(Path(__file__).resolve().parent.parent / ".env")
os.environ.setdefault("GOOGLE_GENAI_USE_VERTEXAI", "FALSE")

MODEL = os.environ.get("GEMINI_MODEL", "gemini-flash-lite-latest")

TASK = """You are a UI-writing assistant inside a Streamlit chat. You answer ONLY by
building small user interfaces with A2UI: a card, a form, a list, a status
panel. Never answer with plain prose alone.

Rules:
- Each user message starts with a line "New surface id: <id>". Use exactly that
  id, as surface("<id>"), for the surface you create in this turn.
- Never create a surface for an id that already exists. To change an existing
  surface, output surface("<its id>", update=true) and then ONLY the variables you change,
  reusing the variable names of the components you replace (for example
  `title = Text("Thank you, Ada")`), plus any new components you add. Do not
  assign `root` again unless you must.
- A form must have a Button whose action is an Event carrying the field values
  in its context. After the user submits, update the same surface to confirm
  what was submitted.
- Do not add validation checks (`?required` and similar).
- Keep every surface compact, with a Card or Column as the root.
"""


@dataclass
class Turn:
    """One chat turn: a user line, or an assistant reply with its own surface log."""

    role: str
    text: str = ""
    surface_id: str | None = None
    log: list[dict[str, Any]] = field(default_factory=list)  # agent -> renderer messages
    notes: list[str] = field(default_factory=list)  # prose the model added
    errors: list[str] = field(default_factory=list)


class ChatAgent:
    def __init__(self, capabilities: dict[str, Any]) -> None:
        caps = read_renderer_capabilities({"a2uiRendererCapabilities": capabilities})
        generator = A2uiGenerator([CatalogConfig(catalog=BasicCatalog("1.0"))])
        self.processor = generator.create_processor(caps)
        # One renderer state for the whole chat: the SDK rejects a re-sent createSurface.
        self.state = self.processor.create_state()
        instruction = TASK + "\n\n" + self.processor.prompt_snippet
        # A callable instruction is not templated, so the JSON braces stay intact.
        self.agent = LlmAgent(name="a2ui_chat", model=MODEL, instruction=lambda _ctx: instruction)
        self.runner = InMemoryRunner(agent=self.agent, app_name="a2ui_chat")
        self.session_id: str | None = None
        self.turns: list[Turn] = []
        self.busy = False
        self._owner: dict[str, Turn] = {}  # surface id -> the turn that shows it
        self._counter = 0
        self._loop = asyncio.new_event_loop()
        threading.Thread(target=self._loop.run_forever, daemon=True).start()

    def ask(self, text: str) -> None:
        """A user chat message: adds a user turn and a fresh assistant turn."""
        self._counter += 1
        surface_id = f"turn-{self._counter}"
        self.turns.append(Turn("user", text))
        reply = Turn("assistant", surface_id=surface_id)
        self.turns.append(reply)
        self._start(f"New surface id: {surface_id}\n{text}", reply)

    def act(self, event: dict[str, Any]) -> None:
        """An action from the renderer goes back to the agent; the reply lands on that surface."""
        surface = (event.get("action") or {}).get("surfaceId")
        target = self._owner.get(surface) or next(t for t in reversed(self.turns) if t.role == "assistant")
        prompt = (
            f"The renderer sent this A2UI message from surface `{surface}`:\n{json.dumps(event)}\n"
            f"Surface `{surface}` already exists: output surface(\"{surface}\", update=true) and reassign only the "
            "variables that change (for example the title Text, as a confirmation, and a new Text "
            "that repeats the submitted values). Do not create the surface again."
        )
        self._start(prompt, target)

    def _start(self, prompt: str, target: Turn) -> None:
        self.busy = True
        asyncio.run_coroutine_threadsafe(self._run(prompt, target), self._loop)

    async def _run(self, prompt: str, target: Turn) -> None:
        try:
            if self.session_id is None:
                session = await self.runner.session_service.create_session(app_name="a2ui_chat", user_id="user")
                self.session_id = session.id
            for attempt in range(4):
                before = len(target.log)
                try:
                    await self._attempt(prompt, target)
                    break
                except Exception as exc:  # noqa: BLE001
                    # Gemini answers 503/429 under load; retry only if nothing was shown yet.
                    transient = any(c in str(exc) for c in ("503", "429", "UNAVAILABLE"))
                    if not transient or attempt == 3 or len(target.log) != before:
                        raise
                    await asyncio.sleep(2 * (attempt + 1))
        except Exception as exc:  # noqa: BLE001 - shown in the chat, not swallowed
            target.errors.append(f"{type(exc).__name__}: {exc}"[:400])
        finally:
            self.busy = False

    async def _attempt(self, prompt: str, target: Turn) -> None:
        stream = self.processor.open_stream(state=self.state, after_rejected_block="continue")
        saw_partial = False
        async for event in self.runner.run_async(
            user_id="user",
            session_id=self.session_id,
            new_message=types.Content(role="user", parts=[types.Part(text=prompt)]),
            run_config=RunConfig(streaming_mode=StreamingMode.SSE),
        ):
            chunk = "".join(p.text or "" for p in (event.content.parts if event.content else []) if not p.thought)
            if not chunk:
                continue
            if event.partial:
                saw_partial = True
            elif saw_partial:
                continue  # the final aggregated event repeats the partial chunks
            self._apply(stream.feed(chunk), target)
        self._apply(stream.close(), target)

    def _apply(self, parts: list[Any], target: Turn) -> None:
        for part in parts:
            if isinstance(part, A2uiPart):
                for message in to_message_dicts(part.a2ui):
                    body = next((v for k, v in message.items() if k != "version"), {})
                    sid = body.get("surfaceId") if isinstance(body, dict) else None
                    owner = self._owner.setdefault(sid, target) if sid else target
                    owner.log.append(message)
            elif isinstance(part, TextPart):
                if part.text.strip():
                    target.notes.append(part.text.strip())
            elif isinstance(part, ErrorPart):
                target.errors.append(f"rejected block: {part.error}"[:400])
