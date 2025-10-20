"""
Duet DSL - Facet-based workflow API.

This module provides the user-facing API for building reactive workflows
with typed facts and facet composition.

Core API:
    facet(name) - Create facet builder
    seq(*facets) - Sequential pipeline
    loop(facet, until=...) - Loop until predicate
    branch(pred, on_true, on_false) - Conditional branching
    once(facet, trigger=...) - One-shot execution
    parallel(*facets) - Parallel execution

Facet Builder Methods:
    .needs(fact_type, alias=None, **constraints) - Declare fact dependency
    .agent(name, *, prompt=None, role=None) - Invoke AI agent
    .tool(tool_instance) - Execute deterministic tool
    .emit(fact_type, *, values, ...) - Emit typed fact
    .human(reason, timeout=None) - Request approval
    .build() - Produce FacetDefinition

Example Workflow:
    from duet.dsl import facet, seq
    from duet.dataspace import TaskRequest, PlanDoc, CodeArtifact

    workflow = seq(
        facet("plan")
            .needs(TaskRequest, alias="task")
            .agent("planner", prompt="Create implementation plan")
            .emit(PlanDoc, values={"content": "$agent_response", "task_id": "$task.fact_id"})
            .build(),

        facet("implement")
            .needs(PlanDoc, alias="plan")
            .agent("coder", prompt="Implement the plan")
            .emit(CodeArtifact, values={"summary": "$agent_response", "plan_id": "$plan.fact_id"})
            .build()
    )
"""

# Core facet builder
from .facet import (
    FacetBuilder,
    FacetDefinition,
    facet,
)

# Combinators
from .combinators import (
    FacetHandle,
    FacetProgram,
    RunPolicy,
    branch,
    loop,
    once,
    parallel,
    seq,
)

# Registry (for advanced use)
from .registry import (
    FacetRegistry,
    clear_registry,
    get_facet,
    get_registry,
    register_facet,
)

# Compiler
from .compiler import (
    compile_handle,
    compile_program,
    validate_and_compile,
)

# Workflow primitives
from .workflow import Phase

# Steps (for advanced custom facets)
from .steps import (
    AgentStep,
    FacetContext,
    HumanStep,
    PhaseStep,
    ReadStep,
    StepResult,
    ToolStep,
    WriteStep,
)

# Tools
from .tools import (
    ApprovalTool,
    BaseTool,
    GitChangeTool,
    Tool,
    ToolContext,
    ToolResult,
    ToolTiming,
)

__all__ = [
    # Core API
    "facet",
    "FacetBuilder",
    "FacetDefinition",
    # Combinators
    "seq",
    "loop",
    "branch",
    "once",
    "parallel",
    "FacetProgram",
    "FacetHandle",
    "RunPolicy",
    # Compiler
    "compile_program",
    "compile_handle",
    "validate_and_compile",
    # Registry
    "FacetRegistry",
    "register_facet",
    "get_facet",
    "get_registry",
    "clear_registry",
    # Workflow primitives
    "Phase",
    # Steps (advanced)
    "ReadStep",
    "AgentStep",
    "ToolStep",
    "WriteStep",
    "HumanStep",
    "FacetContext",
    "StepResult",
    "PhaseStep",
    # Tools
    "Tool",
    "BaseTool",
    "ToolContext",
    "ToolResult",
    "ToolTiming",
    "GitChangeTool",
    "ApprovalTool",
]
