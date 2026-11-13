"""Minimal scan-removal planning for rescan conflicts.

Every accepted scan is an indivisible unit: a spread scan such as ``2-3``
cannot be removed for only one of its pages.  Given the accepted scans of an
issue, :func:`plan_removals` picks the smallest set of scans to remove so
that every page that had coverage keeps exactly one scan.  Pages that never
had coverage stay "pending rescan" (待补扫) and are not part of the plan.

Scans that share pages form connected components; each component is solved
independently with branch and bound (maximising the number of kept scans,
which is the same as minimising removals).  When the constraints of one
component cannot be satisfied simultaneously — any choice either leaves a
page duplicated or turns it into a gap — the involved pages and scans are
reported as blocked instead of emitting a made-up solution.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # avoid a runtime import cycle with app.scraper
    from .scraper import ScanSource

# Hard cap on branch-and-bound nodes shared by all components of one issue,
# so a pathological overlap pattern can never stall the response.  When the
# budget is exhausted the affected components are reported as blocked with an
# honest reason instead of a guessed plan.
MAX_SEARCH_NODES = 50_000

INFEASIBLE_REASON = "这些页面的扫描互相牵连，无法让每页恰好保留一份。"
BUDGET_REASON = "牵连组合过多，超出计算限额，未给出撤下建议。"


@dataclass
class RemovalComponent:
    """A group of pages/scans whose constraints cannot be satisfied at once."""

    pages: list[int]
    scans: list[ScanSource]
    reason: str


@dataclass
class RemovalPlan:
    """Removal suggestion for one issue.

    ``available`` is False while any directory or detail fetch failed: the
    crawl is incomplete, so no definite suggestion is made and the per-page
    verdicts remain the source of truth.  When available, ``keep``/``remove``
    list the scans of every solvable component and ``blocked`` lists the
    components that have no exact solution.
    """

    available: bool
    keep: list[ScanSource] = field(default_factory=list)
    remove: list[ScanSource] = field(default_factory=list)
    blocked: list[RemovalComponent] = field(default_factory=list)
    covered_pages: int = 0
    note: str = ""

    @property
    def has_blocked(self) -> bool:
        return bool(self.blocked)

    @property
    def decision_by_code(self) -> dict[str, str]:
        """Map scan code to ``keep`` / ``remove`` / ``blocked`` for display."""
        decision = {scan.code: "keep" for scan in self.keep}
        decision.update({scan.code: "remove" for scan in self.remove})
        for component in self.blocked:
            for scan in component.scans:
                decision[scan.code] = "blocked"
        return decision


class _BudgetExceeded(Exception):
    """The shared search budget ran out before the component was solved."""


class _ComponentSolver:
    """Exact-cover branch and bound over one connected component.

    Finds a set of scans to KEEP such that every page of the component is
    covered by exactly one kept scan, maximising the number of kept scans
    (equivalently: minimising removals).  ``best_kept`` holds local indices.
    """

    def __init__(self, scan_pages: list[frozenset[int]], budget: list[int]):
        self.scan_pages = scan_pages
        self.page_to_scans: dict[int, list[int]] = {}
        for index, pages in enumerate(scan_pages):
            for page in pages:
                self.page_to_scans.setdefault(page, []).append(index)
        self.pages = sorted(self.page_to_scans)
        self.budget = budget
        self.best_kept: frozenset[int] | None = None

    def solve(self) -> None:
        kept: set[int] = set()
        removed: set[int] = set()
        page_kept = {page: 0 for page in self.pages}
        self._search(kept, removed, page_kept, list(self.pages))

    def _propagate(
        self,
        kept: set[int],
        removed: set[int],
        page_kept: dict[int, int],
        queue: list[int],
    ) -> bool:
        """Apply forced moves until fixpoint; False means contradiction."""
        while queue:
            page = queue.pop()
            count = page_kept[page]
            if count > 1:
                return False
            holders = self.page_to_scans[page]
            if count == 1:
                # The page already has its keeper: every other scan covering
                # it must be removed (a scan is removed as a whole).
                for index in holders:
                    if index not in kept and index not in removed:
                        removed.add(index)
                        queue.extend(self.scan_pages[index])
                continue
            candidates = [i for i in holders if i not in kept and i not in removed]
            if not candidates:
                return False  # nobody left to cover this page
            if len(candidates) == 1:
                index = candidates[0]
                kept.add(index)
                for other in self.scan_pages[index]:
                    page_kept[other] += 1
                    queue.append(other)
        return True

    def _search(
        self,
        kept: set[int],
        removed: set[int],
        page_kept: dict[int, int],
        queue: list[int],
    ) -> None:
        self.budget[0] -= 1
        if self.budget[0] < 0:
            raise _BudgetExceeded
        if not self._propagate(kept, removed, page_kept, queue):
            return

        undecided = [
            index
            for index in range(len(self.scan_pages))
            if index not in kept and index not in removed
        ]
        if not undecided:
            # Propagation guarantees every page is kept exactly once here.
            if self.best_kept is None or len(kept) > len(self.best_kept):
                self.best_kept = frozenset(kept)
            return

        # Upper bound: each further kept scan must cover at least one page
        # that has no keeper yet, and must currently be undecided.
        uncovered = sum(1 for page in self.pages if page_kept[page] == 0)
        bound = len(kept) + min(len(undecided), uncovered)
        if self.best_kept is not None and bound <= len(self.best_kept):
            return

        # Branch on the uncovered page with the fewest remaining candidates.
        branch_candidates: list[int] | None = None
        for page in self.pages:
            if page_kept[page] != 0:
                continue
            candidates = [
                i for i in self.page_to_scans[page] if i not in kept and i not in removed
            ]
            if branch_candidates is None or len(candidates) < len(branch_candidates):
                branch_candidates = candidates
                if len(candidates) == 2:
                    break
        if not branch_candidates:  # pragma: no cover - propagation prevents this
            return

        # Prefer scans covering fewer pages: they lead to max-keep plans early.
        branch_candidates.sort(key=lambda i: (len(self.scan_pages[i]), i))
        for index in branch_candidates:
            new_kept = set(kept)
            new_removed = set(removed)
            new_counts = dict(page_kept)
            new_kept.add(index)
            if any(new_counts[p] > 0 for p in self.scan_pages[index]):
                continue  # would double-cover a page
            for page in self.scan_pages[index]:
                new_counts[page] += 1
            self._search(new_kept, new_removed, new_counts, list(self.scan_pages[index]))


def _components(scan_pages: list[frozenset[int]]) -> list[list[int]]:
    """Group scan indices into components linked by shared pages."""
    parent = list(range(len(scan_pages)))

    def find(index: int) -> int:
        while parent[index] != index:
            parent[index] = parent[parent[index]]
            index = parent[index]
        return index

    first_seen: dict[int, int] = {}
    for index, pages in enumerate(scan_pages):
        for page in pages:
            if page in first_seen:
                left, right = find(first_seen[page]), find(index)
                if left != right:
                    parent[left] = right
            else:
                first_seen[page] = index

    groups: dict[int, list[int]] = {}
    for index in range(len(scan_pages)):
        groups.setdefault(find(index), []).append(index)
    return sorted(groups.values(), key=lambda group: group[0])


def _blocked_component(
    group: list[int], scans: list[ScanSource], reason: str
) -> RemovalComponent:
    pages = sorted({page for index in group for page in scans[index].page_numbers})
    return RemovalComponent(
        pages=pages,
        scans=[scans[index] for index in group],
        reason=reason,
    )


def plan_removals(scans: list[ScanSource]) -> RemovalPlan:
    """Choose the smallest set of scans to remove.

    Every page covered by ``scans`` must end up covered by exactly one kept
    scan.  Scans are indivisible: a spread is kept or removed as a whole.
    Components without an exact solution are reported as blocked.
    """
    covered = sorted({page for scan in scans for page in scan.page_numbers})
    plan = RemovalPlan(available=True, covered_pages=len(covered))
    if not scans:
        plan.note = "本期没有有效扫描，无需撤下；所有页面均为待补扫。"
        return plan

    scan_pages = [frozenset(scan.page_numbers) for scan in scans]
    budget = [MAX_SEARCH_NODES]
    keep_indices: set[int] = set()
    remove_indices: set[int] = set()
    budget_exhausted = False

    for group in _components(scan_pages):
        if len(group) == 1:
            # Shares no page with any other scan: it is the only possible
            # keeper for its pages.
            keep_indices.add(group[0])
            continue
        if budget_exhausted:
            plan.blocked.append(_blocked_component(group, scans, BUDGET_REASON))
            continue
        solver = _ComponentSolver([scan_pages[index] for index in group], budget)
        try:
            solver.solve()
        except _BudgetExceeded:
            budget_exhausted = True
            plan.blocked.append(_blocked_component(group, scans, BUDGET_REASON))
            continue
        if solver.best_kept is None:
            plan.blocked.append(_blocked_component(group, scans, INFEASIBLE_REASON))
            continue
        for local, index in enumerate(group):
            if local in solver.best_kept:
                keep_indices.add(index)
            else:
                remove_indices.add(index)

    plan.keep = [scan for index, scan in enumerate(scans) if index in keep_indices]
    plan.remove = [scan for index, scan in enumerate(scans) if index in remove_indices]

    if plan.blocked:
        blocked_pages = sorted({page for component in plan.blocked for page in component.pages})
        plan.note = (
            f"无牵连的部分建议撤下 {len(plan.remove)} 份、保留 {len(plan.keep)} 份；"
            f"第 {'、'.join(str(page) for page in blocked_pages)} 页的约束无法同时满足，"
            "相关扫描已单独列出，需人工处理。"
        )
    elif plan.remove:
        plan.note = (
            f"建议撤下 {len(plan.remove)} 份、保留 {len(plan.keep)} 份，"
            f"原本有覆盖的 {plan.covered_pages} 页每页恰好保留一份。"
        )
    else:
        plan.note = "每页已恰好由一份扫描覆盖，无需撤下。"
    return plan


def incomplete_crawl_plan() -> RemovalPlan:
    """Plan placeholder used while directory/detail fetches are incomplete."""
    return RemovalPlan(
        available=False,
        note="目录或详情抓取未完成，不提供撤下建议；请先处理抓取失败后重新验收。",
    )
