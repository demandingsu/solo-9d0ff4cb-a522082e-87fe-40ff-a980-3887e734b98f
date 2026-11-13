import httpx
import pytest

import app.removal as removal
from app.removal import plan_removals
from app.scraper import ScanSource, inspect_issue
from sample_site.main import app as sample_app


def make_client() -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=sample_app),
        base_url="http://sample.test",
        follow_redirects=True,
    )


def make_scan(code: str, *pages: int) -> ScanSource:
    return ScanSource(
        code=code,
        detail_url=f"http://sample.test/scan/{code}.html",
        image_url=f"http://sample.test/img/{code}.svg",
        pages_text="-".join(str(page) for page in pages),
        page_numbers=sorted(set(pages)),
        issue="2024-05-01",
        edition="第3版",
    )


def codes(scans) -> list[str]:
    return [scan.code for scan in scans]


def assert_exact_cover(plan) -> None:
    """Every page covered by any scan keeps exactly one kept scan."""
    kept_coverage: dict[int, int] = {}
    for scan in plan.keep:
        for page in scan.page_numbers:
            kept_coverage[page] = kept_coverage.get(page, 0) + 1
    all_pages = {
        page
        for scan in list(plan.keep) + list(plan.remove)
        for page in scan.page_numbers
    }
    assert kept_coverage == {page: 1 for page in all_pages}


def test_single_conflict_removes_exactly_one_scan():
    plan = plan_removals([make_scan("S01", 5), make_scan("S02", 5)])

    assert plan.available
    assert not plan.has_blocked
    assert len(plan.remove) == 1
    assert len(plan.keep) == 1
    # deterministic: the earlier scan is kept
    assert codes(plan.keep) == ["S01"]
    assert codes(plan.remove) == ["S02"]
    assert_exact_cover(plan)


def test_spread_scan_is_indivisible_and_forces_single_page_out():
    # Removing the spread would orphan page 1, so the single-page scan goes.
    plan = plan_removals([make_scan("S01", 1, 2), make_scan("S02", 2)])

    assert codes(plan.keep) == ["S01"]
    assert codes(plan.remove) == ["S02"]
    assert_exact_cover(plan)


def test_minimal_removal_prefers_removing_the_spread():
    # Keeping the spread would force removing both single-page scans (2
    # removals); removing the spread alone is the minimal plan (1 removal).
    plan = plan_removals(
        [make_scan("S01", 1, 2), make_scan("S02", 1), make_scan("S03", 2)]
    )

    assert codes(plan.remove) == ["S01"]
    assert codes(plan.keep) == ["S02", "S03"]
    assert_exact_cover(plan)


def test_entangled_chain_removes_middle_spread():
    plan = plan_removals(
        [
            make_scan("S01", 1, 2),
            make_scan("S02", 2, 3),
            make_scan("S03", 3, 4),
        ]
    )

    assert codes(plan.keep) == ["S01", "S03"]
    assert codes(plan.remove) == ["S02"]
    assert_exact_cover(plan)


def test_infeasible_component_is_blocked_without_fake_plan():
    plan = plan_removals(
        [
            make_scan("S01", 1, 2),
            make_scan("S02", 2, 3),
            make_scan("S03", 2, 3),
        ]
    )

    assert plan.has_blocked
    assert plan.keep == []
    assert plan.remove == []
    assert len(plan.blocked) == 1
    component = plan.blocked[0]
    assert component.pages == [1, 2, 3]
    assert codes(component.scans) == ["S01", "S02", "S03"]
    assert component.reason
    assert plan.decision_by_code == {"S01": "blocked", "S02": "blocked", "S03": "blocked"}


def test_blocked_component_does_not_hide_solvable_parts():
    plan = plan_removals(
        [
            make_scan("S01", 1, 2),
            make_scan("S02", 2, 3),
            make_scan("S03", 2, 3),
            make_scan("S04", 4),
            make_scan("S05", 5),
            make_scan("S06", 5),
        ]
    )

    assert codes(plan.keep) == ["S04", "S05"]
    assert codes(plan.remove) == ["S06"]
    assert [component.pages for component in plan.blocked] == [[1, 2, 3]]
    assert_exact_cover(plan)


def test_plan_without_scans_is_empty_but_available():
    plan = plan_removals([])

    assert plan.available
    assert plan.keep == []
    assert plan.remove == []
    assert not plan.has_blocked
    assert "待补扫" in plan.note


def test_budget_exhaustion_blocks_component_instead_of_guessing(monkeypatch):
    monkeypatch.setattr(removal, "MAX_SEARCH_NODES", 0)
    plan = plan_removals([make_scan("S01", 1, 2), make_scan("S02", 2, 3)])

    assert plan.has_blocked
    assert plan.keep == []
    assert plan.remove == []
    assert "限额" in plan.blocked[0].reason


@pytest.mark.asyncio
async def test_clean_issue_plan_keeps_everything():
    async with make_client() as client:
        report = await inspect_issue(
            "http://sample.test/issues/clean/page/1.html", client=client
        )

    plan = report.removal_plan
    assert plan is not None and plan.available
    assert plan.remove == []
    assert not plan.has_blocked
    assert codes(plan.keep) == codes(report.accepted_scans)
    assert "无需撤下" in plan.note


@pytest.mark.asyncio
async def test_conflict_issue_plan_removes_single_page_duplicate():
    async with make_client() as client:
        report = await inspect_issue(
            "http://sample.test/issues/conflict/page/1.html", client=client
        )

    plan = report.removal_plan
    assert plan is not None and plan.available
    assert not plan.has_blocked
    # page 2 is only covered by the 2-3 spread, so the spread stays and the
    # single-page duplicate of page 3 is the one to remove
    removed = {scan.code: scan for scan in plan.remove}
    assert list(removed) == ["S03"]
    assert removed["S03"].pages_text == "3"
    kept_texts = sorted(scan.pages_text for scan in plan.keep)
    assert kept_texts == ["1", "2-3", "6-7", "8"]
    assert_exact_cover(plan)
    # originally missing pages stay pending rescan
    assert report.missing_pages == [4, 5]
    assert plan.covered_pages == 6


@pytest.mark.asyncio
async def test_pending_issue_has_no_definite_plan():
    async with make_client() as client:
        report = await inspect_issue(
            "http://sample.test/issues/pending/page/1.html", client=client
        )

    plan = report.removal_plan
    assert plan is not None
    assert not plan.available
    assert plan.keep == [] and plan.remove == [] and not plan.has_blocked
    assert "抓取" in plan.note
    # per-page verdicts are still produced
    assert report.conflict_pages == [4]
    assert report.missing_pages == [5, 9, 11, 12]


@pytest.mark.asyncio
async def test_tangle_issue_plan_removes_two_spreads():
    async with make_client() as client:
        report = await inspect_issue(
            "http://sample.test/issues/tangle/page/1.html", client=client
        )

    assert report.status == "不合格"
    assert report.conflict_pages == [2, 3, 5, 6]
    plan = report.removal_plan
    assert plan is not None and plan.available
    assert not plan.has_blocked
    # minimal plan: remove the two middle spreads, keep four scans
    assert codes(plan.remove) == ["S02", "S05"]
    assert codes(plan.keep) == ["S01", "S03", "S04", "S06"]
    assert_exact_cover(plan)
    assert plan.covered_pages == 6


@pytest.mark.asyncio
async def test_unsolvable_issue_lists_pages_and_scans_without_fake_plan():
    async with make_client() as client:
        report = await inspect_issue(
            "http://sample.test/issues/unsolvable/page/1.html", client=client
        )

    assert report.status == "不合格"
    plan = report.removal_plan
    assert plan is not None and plan.available
    assert plan.has_blocked
    assert len(plan.blocked) == 1
    component = plan.blocked[0]
    assert component.pages == [1, 2, 3]
    assert codes(component.scans) == ["S01", "S02", "S03"]
    # the untouched page 4 scan is still kept; nothing is removed blindly
    assert codes(plan.keep) == ["S04"]
    assert plan.remove == []
    assert plan.decision_by_code == {
        "S01": "blocked",
        "S02": "blocked",
        "S03": "blocked",
        "S04": "keep",
    }
