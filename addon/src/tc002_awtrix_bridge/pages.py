from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .errors import not_found, validation
from .validation import validate_app_name


@dataclass(slots=True)
class Page:
    name: str
    spec: dict[str, Any]
    origin: str = "pushed"
    received_ms: int = 0
    marked_expired: bool = False


@dataclass(slots=True)
class PageManager:
    pages: dict[str, Page] = field(default_factory=dict)
    order: list[str] = field(default_factory=lambda: ["Time", "Date"])
    disabled: set[str] = field(default_factory=set)
    current_name: str = "Time"
    page_started_ms: int = 0

    def __post_init__(self) -> None:
        self.pages.setdefault("Time", Page("Time", {}, origin="builtin"))
        self.pages.setdefault("Date", Page("Date", {}, origin="builtin"))

    def visible_names(self) -> list[str]:
        ordered = [name for name in self.order if name in self.pages and name not in self.disabled]
        newcomers = [
            name for name in self.pages if name not in self.order and name not in self.disabled
        ]
        return ordered + newcomers

    @property
    def current(self) -> Page:
        if self.current_name not in self.pages or self.current_name in self.disabled:
            names = self.visible_names()
            self.current_name = names[0] if names else "Time"
        return self.pages[self.current_name]

    def set_order(self, order: list[str], disabled: list[str] | None = None) -> None:
        if not isinstance(order, list) or not all(
            isinstance(name, str) and bool(name) for name in order
        ):
            raise validation("App order must be an array of names", "order")
        disabled = disabled or []
        if not isinstance(disabled, list) or not all(
            isinstance(name, str) and bool(name) for name in disabled
        ):
            raise validation("disabled must be an array of names", "disabled")
        self.order = order.copy()
        self.disabled = set(disabled)
        _ = self.current

    def upsert(self, name: str, spec: dict[str, Any], now_ms: int, origin: str = "pushed") -> Page:
        validate_app_name(name)
        page = Page(name=name, spec=spec.copy(), origin=origin, received_ms=now_ms)
        self.pages[name] = page
        if name not in self.order:
            self.order.append(name)
        return page

    def delete_base(self, base: str) -> int:
        validate_app_name(base)
        targets = {
            name
            for name, page in self.pages.items()
            if (name == base or self._numbered_child(name, base)) and page.origin != "builtin"
        }
        targets.update(
            name
            for name in self.order
            if (name == base or self._numbered_child(name, base))
            and (name not in self.pages or self.pages[name].origin != "builtin")
        )
        for name in targets:
            self.pages.pop(name, None)
        self.order = [name for name in self.order if name not in targets]
        self.disabled.difference_update(targets)
        if self.current_name in targets:
            names = self.visible_names()
            self.current_name = names[0] if names else "Time"
        return len(targets)

    @staticmethod
    def _numbered_child(name: str, base: str) -> bool:
        suffix = name[len(base) :] if name.startswith(base) else ""
        return bool(suffix) and suffix.isdigit()

    def switch(self, name: str, now_ms: int) -> Page:
        validate_app_name(name)
        if name not in self.pages or name in self.disabled:
            raise not_found(f"App not found or disabled: {name}", "name")
        self.current_name = name
        self.page_started_ms = now_ms
        return self.pages[name]

    def step(self, delta: int, now_ms: int) -> Page:
        names = self.visible_names()
        if not names:
            raise not_found("No enabled apps")
        try:
            index = names.index(self.current_name)
        except ValueError:
            index = 0
        self.current_name = names[(index + delta) % len(names)]
        self.page_started_ms = now_ms
        return self.pages[self.current_name]

    def expire_lifetimes(self, now_ms: int) -> list[str]:
        removed: list[str] = []
        for name, page in list(self.pages.items()):
            lifetime = page.spec.get("lifetimeMs")
            if page.origin != "pushed" or not isinstance(lifetime, int) or lifetime <= 0:
                continue
            if now_ms - page.received_ms < lifetime:
                continue
            if page.spec.get("lifetimeExpiry", "remove") == "mark":
                page.marked_expired = True
            else:
                del self.pages[name]
                removed.append(name)
        if self.current_name not in self.pages:
            names = self.visible_names()
            self.current_name = names[0] if names else "Time"
            self.page_started_ms = now_ms
        return removed

    def inventory(self) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = []
        for name, page in self.pages.items():
            result.append(
                {
                    "name": name,
                    "enabled": name not in self.disabled,
                    "inLoop": name in self.order,
                    "slot": self.order.index(name) if name in self.order else None,
                    "present": True,
                    "origin": page.origin,
                }
            )
        for name in self.order:
            if name not in self.pages:
                result.append(
                    {
                        "name": name,
                        "enabled": name not in self.disabled,
                        "inLoop": True,
                        "slot": self.order.index(name),
                        "present": False,
                        "origin": None,
                    }
                )
        return result
