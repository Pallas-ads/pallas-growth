"""Picks a model per role, so one run can mix providers (text roles on Baseten, web search on Meta)."""

from __future__ import annotations

from dataclasses import dataclass, field

from pydantic_ai.models import Model

# Roles that need a Meta-only hosted tool. Everything else is plain text, tools and images.
META_ONLY = frozenset({"strategist"})


@dataclass
class RoleRouter:
    """`default` serves any role not named in `by_role`."""

    default: Model
    by_role: dict[str, Model] = field(default_factory=dict)

    def for_role(self, role: str) -> Model:
        return self.by_role.get(role, self.default)

    @property
    def model_name(self) -> str:
        return self.default.model_name


def mixed(meta: Model, text: Model) -> RoleRouter:
    """Text roles on `text`; roles in META_ONLY on `meta`."""
    return RoleRouter(default=text, by_role={role: meta for role in META_ONLY})


def resolve(model: Model | RoleRouter, role: str) -> Model:
    return model.for_role(role) if isinstance(model, RoleRouter) else model
