"""Main-agent graph factory wiring orchestrator nodes + sub-agent subgraph."""
from __future__ import annotations

import logging

from langgraph.graph import END, START, StateGraph

from chat.backend.agent.orchestrator.nodes import (
    finalize_node,
    plan_node,
    route_after_plan,
    route_after_synthesize,
    synthesize_or_replan_node,
    triage_node,
)
from chat.backend.agent.orchestrator.state import MainAgentState
from chat.backend.agent.subagent.graph import build_subagent_subgraph

logger = logging.getLogger(__name__)


def build_main_agent_graph(checkpointer=None):
    subagent_subgraph = build_subagent_subgraph(delegate_level=1)

    graph: StateGraph = StateGraph(MainAgentState)
    graph.add_node("triage", triage_node)
    graph.add_node("plan", plan_node)
    graph.add_node("subagent", subagent_subgraph)
    graph.add_node("synthesize_or_replan", synthesize_or_replan_node)
    graph.add_node("finalize", finalize_node)

    graph.add_edge(START, "triage")
    graph.add_edge("triage", "plan")
    graph.add_conditional_edges("plan", route_after_plan, ["subagent", END])
    graph.add_edge("subagent", "synthesize_or_replan")
    graph.add_conditional_edges(
        "synthesize_or_replan", route_after_synthesize, ["subagent", "finalize"],
    )
    graph.add_edge("finalize", END)

    return graph.compile(checkpointer=checkpointer) if checkpointer else graph.compile()
