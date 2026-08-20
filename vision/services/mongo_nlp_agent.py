"""
[OPTIONAL — secondary to standard MongoDB access]
=====================================================

WHY THIS FILE EXISTS
---------------------
Supersedes vision/services/deepseek_query.py's homegrown "prompt ->
strict-JSON-filter -> validate -> find_objects()" approach with the
officially maintained langchain-mongodb agent toolkit (MongoDB +
LangChain jointly maintain it — see mongodb-nlp-query-summary.txt).
Gets you the toolkit's built-in query VALIDATOR tool, collection/schema
discovery, and (via MongoDBSaver) multi-turn conversational querying,
instead of hand-rolled JSON repair/validation.

THIS STAYS SECONDARY, ON PURPOSE
-----------------------------------
The Database tab's "Objects" / "Images" / "Inventory" browser goes
straight through vision.storage.mongo_client — it NEVER touches this
module or any LLM. This file only powers the optional "Ask" box layered
on top, invoked only when the user explicitly types a natural-language
question. If Ollama isn't running, none of the standard browsing
functionality is affected.

TOOL CALLING REQUIREMENT
--------------------------
The toolkit's agent depends on the model reliably emitting tool calls.
Reasoning models (deepseek-r1) and small (<7B) general models do NOT do
this reliably (see mongodb-nlp-query-summary.txt) — that's why this
module defaults to vision.config.NLP_AGENT_MODEL ("qwen2.5:7b"), not
OLLAMA_MODEL (which is deepseek-r1:7b, kept only for the old, now-
superseded deepseek_query.py).

SETUP
-----
    pip install -U langchain-mongodb langchain-ollama langgraph
    ollama pull qwen2.5:7b

None of this is required for the rest of the app — every function here
fails soft (raises a caught, displayable exception) so main.py can
disable the "Ask" button / show an error instead of crashing when
Ollama isn't running, the model isn't pulled, or the langchain-mongodb
packages aren't installed.
"""

from __future__ import annotations

import requests

from vision.config import MONGO_URI, MONGO_DB_NAME, OLLAMA_HOST, NLP_AGENT_MODEL

try:
    from langchain_mongodb.agent_toolkit.database import MongoDBDatabase
    from langchain_mongodb.agent_toolkit.toolkit import MongoDBDatabaseToolkit
    from langchain_ollama import ChatOllama
    from langgraph.prebuilt import create_react_agent
    _AGENT_DEPS_AVAILABLE = True
except ImportError:
    _AGENT_DEPS_AVAILABLE = False


class MongoNLPAgentError(Exception):
    """Raised for anything that should be shown to the user as a plain
    error message rather than crashing the GUI — missing deps, Ollama
    unreachable, model not pulled, agent failure, etc."""


# Models known to reliably support tool calling (required for this
# toolkit's agent loop) at a size that's practical to run locally, per
# mongodb-nlp-query-summary.txt. Shown in the GUI as "not installed yet
# but recommended" alongside whatever's actually pulled in Ollama, each
# with a one-line reason and the exact `ollama pull` command.
RECOMMENDED_MODELS = [
    {"name": "qwen2.5:7b", "note": "Good default — reliable tool calling, modest size."},
    {"name": "qwen3:14b", "note": "Stronger reasoning than qwen2.5:7b, needs more RAM/VRAM."},
    {"name": "llama3.1:8b", "note": "Solid alternative to qwen2.5:7b, similar size class."},
    {"name": "llama4:scout", "note": "Newer/larger — best accuracy if your hardware can run it."},
    {"name": "mistral-small", "note": "Lighter-weight option, recent versions only."},
]


def list_installed_models() -> list[str]:
    """
    Every model currently pulled in the local Ollama install (e.g.
    ["qwen2.5:7b", "deepseek-r1:7b", ...]), for populating the GUI's
    model-selection dropdown. Returns [] (not an exception) if Ollama
    isn't reachable — callers should treat an empty list as "show the
    recommendations, nothing's installed / can't tell yet" rather than
    a hard failure, since this is meant to be safe to call opportunistically
    (e.g. every time the Database tab is opened) without risking a crash.
    """
    try:
        resp = requests.get(f"{OLLAMA_HOST}/api/tags", timeout=5)
        resp.raise_for_status()
        return sorted(m.get("name", "") for m in resp.json().get("models", []) if m.get("name"))
    except (requests.RequestException, ValueError, AttributeError):
        return []


# Lazily built — constructing the agent talks to Ollama and Mongo, so
# this only happens on first actual use (or explicit availability
# check), never at import time.
_agent = None
_agent_model = None


def check_agent_available(model: str = NLP_AGENT_MODEL) -> None:
    """
    Raises MongoNLPAgentError with a human-readable reason if the
    langchain-mongodb packages aren't installed, Ollama isn't reachable,
    or the requested model hasn't been pulled. Call this before enabling
    any NL-query UI so it can be disabled instead of failing on first
    use — mirrors deepseek_query.check_ollama_available()'s role.
    """
    if not _AGENT_DEPS_AVAILABLE:
        raise MongoNLPAgentError(
            "langchain-mongodb agent toolkit isn't installed.\n"
            "Run: pip install -U langchain-mongodb langchain-ollama langgraph"
        )

    try:
        resp = requests.get(f"{OLLAMA_HOST}/api/tags", timeout=5)
        resp.raise_for_status()
    except requests.RequestException as e:
        raise MongoNLPAgentError(f"Can't reach Ollama at {OLLAMA_HOST}: {e}\nIs `ollama serve` running?")

    try:
        available = {m.get("name", "") for m in resp.json().get("models", [])}
    except (ValueError, AttributeError):
        available = set()

    if available and not any(m.startswith(model) for m in available):
        raise MongoNLPAgentError(
            f"Model '{model}' isn't pulled in Ollama.\n"
            f"Run: ollama pull {model}\n"
            f"(This must be a tool-calling-capable model — qwen2.5:7b+ or "
            f"llama3.1:8b+. Reasoning/small models don't reliably support "
            f"the tool calls this agent needs.)"
        )


def _get_agent(model: str = NLP_AGENT_MODEL):
    global _agent, _agent_model
    if _agent is not None and _agent_model == model:
        return _agent

    if not _AGENT_DEPS_AVAILABLE:
        raise MongoNLPAgentError(
            "langchain-mongodb agent toolkit isn't installed.\n"
            "Run: pip install -U langchain-mongodb langchain-ollama langgraph"
        )

    try:
        db = MongoDBDatabase.from_connection_string(f"{MONGO_URI}/{MONGO_DB_NAME}")
        llm = ChatOllama(model=model, temperature=0)
        toolkit = MongoDBDatabaseToolkit(db=db, llm=llm)
        _agent = create_react_agent(llm, toolkit.get_tools())
        _agent_model = model
    except Exception as e:
        raise MongoNLPAgentError(f"Could not start the NL query agent: {e}")

    return _agent


def ask(question: str, model: str = NLP_AGENT_MODEL) -> str:
    """
    Runs a natural-language question through the langchain-mongodb
    ReAct agent (generate -> validate -> execute, all handled by the
    toolkit) against the live `objects`/`images` collections. Returns
    the agent's final text answer. Raises MongoNLPAgentError on any
    failure along the way — main.py should catch this and show it as an
    inline error rather than crashing the GUI.

    Unlike the old deepseek_query.run_nl_query(), this does not return a
    raw Mongo filter/result list to re-populate the listbox with — the
    agent's own generated -> validated -> executed query result is
    summarized in its final answer. If you need the raw matching
    documents back in the GUI list (not just a text answer), fall back
    to vision.storage.mongo_client.find_objects() with a filter you
    write by hand, or extend this function to also return the toolkit's
    last query result alongside the text answer.
    """
    agent = _get_agent(model)
    try:
        response = agent.invoke({"messages": [("user", question)]})
    except Exception as e:
        raise MongoNLPAgentError(f"Query failed: {e}")

    messages = response.get("messages", [])
    if not messages:
        raise MongoNLPAgentError("The agent returned no answer.")
    return messages[-1].content
