"""A name → :class:`~app.sagas.definition.Workflow` registry.

The durable store persists only a workflow *name* (definitions hold live Python
callables and can't be serialised). On resume the engine looks the name up here
to recover the actions/compensations. Keeping the registry explicit means a
resumed run is bound to the *current* code's definition of that workflow — the
intended behaviour for a code-deploy-then-resume.
"""

from __future__ import annotations

from app.sagas.definition import Workflow
from app.sagas.errors import UnknownWorkflowError, WorkflowDefinitionError


class WorkflowRegistry:
    """Holds the workflow definitions an engine can run/resume."""

    __slots__ = ("_workflows",)

    def __init__(self, workflows: list[Workflow] | None = None) -> None:
        self._workflows: dict[str, Workflow] = {}
        for wf in workflows or []:
            self.register(wf)

    def register(self, workflow: Workflow) -> Workflow:
        if workflow.name in self._workflows:
            raise WorkflowDefinitionError(f"workflow {workflow.name!r} already registered")
        # validate() runs in Workflow.__post_init__, but re-assert on register.
        workflow.validate()
        self._workflows[workflow.name] = workflow
        return workflow

    def get(self, name: str) -> Workflow:
        try:
            return self._workflows[name]
        except KeyError as exc:
            raise UnknownWorkflowError(name) from exc

    def names(self) -> list[str]:
        return sorted(self._workflows)

    def __contains__(self, name: str) -> bool:
        return name in self._workflows


__all__ = ["WorkflowRegistry"]
