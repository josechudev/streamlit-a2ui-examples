"""Chat where a Gemini model, via a Google ADK agent, writes the UI as A2UI.

Run:  streamlit run chat/app.py
"""

from __future__ import annotations

import json

import streamlit as st
from agent import MODEL, ChatAgent

from streamlit_a2ui import a2ui_surface, renderer_capabilities

st.set_page_config(page_title="A2UI chat", page_icon="💬")
st.title("A2UI chat")
st.caption(f"A real model writes the UI. `{MODEL}` · Google ADK · a2ui-agent-sdk · streamlit-a2ui")

if "agent" not in st.session_state:
    st.session_state["agent"] = ChatAgent(renderer_capabilities())
agent: ChatAgent = st.session_state["agent"]

if prompt := st.chat_input("Ask for a card or a form, e.g. “a form to book a table for two”"):
    agent.ask(prompt)


@st.fragment(run_every=0.5)
def conversation() -> None:
    """Redraws every tick, so messages the agent thread appends show up as they arrive."""
    if not agent.turns:
        st.info("Try: “I need a card saying Ship 0.1.0” or “a form to book a table for two”.")
    for turn in list(agent.turns):
        if turn.role == "user":
            with st.chat_message("user"):
                st.write(turn.text)
            continue
        with st.chat_message("assistant"):
            log = list(turn.log)
            if log:
                event = a2ui_surface(log, key=f"surface-{turn.surface_id}")
                if event is not None:
                    agent.act(event)  # at most one renderer message per rerun
                with st.expander("A2UI JSON the agent sent", expanded=False):
                    st.code(json.dumps(log, indent=2), language="json")
            elif not turn.errors:
                st.caption("Thinking…")
            for note in turn.notes:
                st.write(note)
            for error in turn.errors:
                st.error(error)


conversation()
