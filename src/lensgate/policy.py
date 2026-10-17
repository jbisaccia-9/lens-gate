"""The access policy: IdP groups -> roles -> connections, schemas and collections."""
from __future__ import annotations

from dataclasses import dataclass
from importlib import resources
from pathlib import Path

import yaml


def _load_yaml(name: str, path: str | Path | None = None) -> dict:
    if path is not None:
        return yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    return yaml.safe_load(resources.files("lensgate.config").joinpath(name).read_text(encoding="utf-8"))


@dataclass(frozen=True)
class Role:
    name: str
    metabase_group: str
    idp_groups: tuple[str, ...]
    connections: tuple[str, ...]
    view: tuple[str, ...]
    curate: tuple[str, ...]


class Policy:
    def __init__(self, raw: dict):
        self.raw = raw
        self.company: str = raw["company"]
        self.identity: dict = raw["identity"]
        self.connections: dict[str, tuple[str, ...]] = {k: tuple(v) for k, v in raw["connections"].items()}
        self.roles: dict[str, Role] = {
            name: Role(
                name=name,
                metabase_group=r["metabase_group"],
                idp_groups=tuple(r["idp_groups"]),
                connections=tuple(r["connections"]),
                view=tuple(r.get("view", [])),
                curate=tuple(r.get("curate", [])),
            )
            for name, r in raw["roles"].items()
        }
        self.collections: list[str] = list(raw["collections"])
        self.service_principals: frozenset[str] = frozenset(raw.get("service_principals", []))
        for r in self.roles.values():
            for c in r.connections:
                if c not in self.connections:
                    raise ValueError(f"role {r.name}: unknown connection {c}")
            for c in r.view + r.curate:
                if c not in self.collections:
                    raise ValueError(f"role {r.name}: unknown collection {c}")

    @classmethod
    def load(cls, path: str | Path | None = None) -> "Policy":
        return cls(_load_yaml("policy.yaml", path))

    # --- identity -> roles ------------------------------------------------
    def roles_for_groups(self, groups: list[str]) -> list[str]:
        """Mapped roles for a groups claim. Unknown groups contribute nothing."""
        gs = set(groups)
        return sorted(r.name for r in self.roles.values() if gs & set(r.idp_groups))

    def role_for_metabase_group(self, group: str) -> str | None:
        for r in self.roles.values():
            if r.metabase_group == group:
                return r.name
        return None

    # --- roles -> grants --------------------------------------------------
    def connections_for(self, roles: list[str]) -> set[str]:
        return {c for r in roles for c in self.roles[r].connections}

    def schemas_for(self, connection: str) -> set[str]:
        return set(self.connections.get(connection, ()))

    def curate_for(self, roles: list[str]) -> set[str]:
        return {c for r in roles for c in self.roles[r].curate}

    def view_for(self, roles: list[str]) -> set[str]:
        return {c for r in roles for c in self.roles[r].view + self.roles[r].curate}


def load_dashboards(path: str | Path | None = None) -> list[dict]:
    return _load_yaml("dashboards.yaml", path)["dashboards"]
