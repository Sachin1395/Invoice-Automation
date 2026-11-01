"""LangGraph wiring: ingest -> extract -> validate -> (autopass | human_review | hard_reject) -> export.

A file can hold several invoices, each with its own decision, so after validation the graph
fans out to every route that at least one invoice needs.
"""
from __future__ import annotations

from functools import partial
from typing import Optional

from langgraph.graph import END, START, StateGraph

from .config import Settings
from .llm.chain import ModelChain
from .models import ExtractionStatus, IngestStatus, PipelineState
from .nodes.export import export_node
from .nodes.extract import extract_node
from .nodes.ingest import ingest_node
from .nodes.routes import autopass_node, hard_reject_node, human_review_node
from .nodes.validate import validate_node
from .profiles.base import Profile
from .validation.bank import BankVerifier


def route_after_ingest(state: PipelineState, extraction_enabled: bool = True) -> str:
    if state.ingest_status == IngestStatus.OK and extraction_enabled:
        return "extract"
    return "stop"


def route_after_extract(state: PipelineState) -> str:
    return "continue" if state.extraction_status == ExtractionStatus.OK else "stop"


def route_after_validate(state: PipelineState) -> list[str]:
    routes: list[str] = []
    for v in state.validations:
        if v["route"] not in routes:
            routes.append(v["route"])
    return routes or ["human_review"]


def build_graph(settings: Settings, chain: Optional[ModelChain] = None, profile: Optional[Profile] = None,
                bank: Optional[BankVerifier] = None):
    g = StateGraph(PipelineState)
    g.add_node("ingest", partial(ingest_node, settings=settings))
    g.add_edge(START, "ingest")

    if not settings.extraction_enabled:
        g.add_edge("ingest", END)
        return g.compile()

    if chain is None or profile is None:
        raise ValueError("extraction is enabled but no model chain / profile was given")
    g.add_node("extract", partial(extract_node, settings=settings, chain=chain, profile=profile))
    g.add_conditional_edges("ingest", partial(route_after_ingest, extraction_enabled=True),
                            {"extract": "extract", "stop": END})

    if not settings.validation_enabled:
        g.add_conditional_edges("extract", route_after_extract, {"continue": END, "stop": END})
        return g.compile()

    g.add_node("validate", partial(validate_node, settings=settings, profile=profile, bank=bank))
    g.add_node("autopass", partial(autopass_node, settings=settings))
    g.add_node("human_review", partial(human_review_node, settings=settings))
    g.add_node("hard_reject", partial(hard_reject_node, settings=settings))
    g.add_conditional_edges("extract", route_after_extract, {"continue": "validate", "stop": END})
    g.add_conditional_edges("validate", route_after_validate,
                            {"autopass": "autopass", "human_review": "human_review", "hard_reject": "hard_reject"})
    if settings.export_enabled:
        g.add_node("export", partial(export_node, settings=settings))
        for node in ("autopass", "human_review", "hard_reject"):
            g.add_edge(node, "export")    # whichever routes ran, export runs once afterwards
        g.add_edge("export", END)
    else:
        for node in ("autopass", "human_review", "hard_reject"):
            g.add_edge(node, END)
    return g.compile()


def run_graph(graph, job_id: int, source_path: str) -> PipelineState:
    result = graph.invoke({"job_id": job_id, "source_path": source_path})
    return result if isinstance(result, PipelineState) else PipelineState.model_validate(result)
