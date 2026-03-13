from __future__ import annotations

"""Contract tests for the workflow state machine, PR↔Issue linker, and invariants.

These tests enforce the acceptance criteria from the Ship board swim-lane
state machine specification.  They must pass before any merge.

Coverage:

1. Open PR with ``Closes #17`` and non-standard branch → lane ``pr_open``
2. Open PR with no body but branch ``ac/issue-17`` → lane ``pr_open``
3. Run has ``pr_number=42`` but PR not yet in DB → warning + deterministic behaviour
4. PR targets ``main`` not ``dev`` → lane ``pr_open`` + warning ``wrong_base``
5. PR merges → lane ``done`` (with stabilisation even if issue still open)
6. Multiple runs: latest unknown, older has PR → lane still correct via PR link
7. Tombstone absence does NOT flip to closed (no tombstone poisoning)
8. Linker discovers all signal types with correct confidence
9. Best-PR selection precedence
10. Invariant checks

Run:
    docker compose exec agentception pytest \\
        agentception/tests/test_workflow_state_machine.py -v
"""

import json

import pytest

from agentception.workflow.linking import (
    BestPR,
    CandidateLink,
    PRInfo,
    PRRow,
    RunRow,
    best_pr_for_issue,
    discover_links_for_pr,
)
from agentception.workflow.state_machine import (
    LANE_ACTIVE,
    LANE_DONE,
    LANE_PR_OPEN,
    LANE_REVIEWING,
    LANE_TODO,
    IssueInput,
    RunInput,
    WorkflowState,
    compute_workflow_state,
)
from agentception.workflow.status import (
    ACTIVE_STATUSES,
    LANE_ACTIVE_STATUSES,
    LIVE_STATUSES,
    RESET_STATUSES,
    AgentStatus,
    compute_agent_status,
    is_active,
    is_live,
)
from agentception.workflow.invariants import (
    InvariantContext,
    WorkflowSnapshot,
    check_invariants,
)

_REPO = "cgcardona/agentception"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _issue(
    number: int = 17,
    state: str = "open",
    labels: list[str] | None = None,
    phase_key: str | None = None,
    initiative: str | None = None,
) -> IssueInput:
    return IssueInput(
        number=number,
        state=state,
        labels=labels or [],
        phase_key=phase_key,
        initiative=initiative,
    )


def _run(
    run_id: str = "issue-17",
    status: str = "implementing",
    agent_status: str = "implementing",
    pr_number: int | None = None,
) -> RunInput:
    return RunInput(
        id=run_id,
        status=status,
        agent_status=agent_status,
        pr_number=pr_number,
    )


def _best_pr(
    pr_number: int = 42,
    pr_state: str = "open",
    pr_base: str | None = "dev",
    pr_head_ref: str | None = "ac/issue-17",
    link_method: str = "body_closes",
    confidence: int = 95,
) -> BestPR:
    return BestPR(
        pr_number=pr_number,
        pr_state=pr_state,
        pr_base=pr_base,
        pr_head_ref=pr_head_ref,
        link_method=link_method,
        confidence=confidence,
    )


def _pr_row(
    number: int = 42,
    title: str = "Fix the thing",
    head_ref: str | None = "ac/issue-17",
    base_ref: str | None = "dev",
    body: str = "",
    labels: list[str] | None = None,
) -> PRRow:
    return PRRow(
        number=number,
        title=title,
        head_ref=head_ref,
        base_ref=base_ref,
        body=body,
        labels=labels or [],
    )


# ===========================================================================
# 1) State Machine — Lane Computation
# ===========================================================================


class TestLaneComputation:
    """Core state machine lane rules."""

    def test_closed_issue_is_done(self) -> None:
        result = compute_workflow_state(_issue(state="closed"), None, None)
        assert result["lane"] == LANE_DONE

    def test_open_pr_with_closes_body_is_pr_open(self) -> None:
        """Acceptance criterion 1: PR with Closes #17 and non-standard branch."""
        result = compute_workflow_state(
            _issue(),
            _run(),
            _best_pr(pr_head_ref="fix/my-thing", link_method="body_closes"),
        )
        assert result["lane"] == LANE_PR_OPEN

    def test_open_pr_with_branch_regex_is_pr_open(self) -> None:
        """Acceptance criterion 2: no body but ac/issue-17 branch."""
        result = compute_workflow_state(
            _issue(),
            _run(),
            _best_pr(link_method="branch_regex"),
        )
        assert result["lane"] == LANE_PR_OPEN

    def test_pr_wrong_base_still_pr_open_with_warning(self) -> None:
        """Acceptance criterion 4: PR targets main, still shows in PR lane."""
        result = compute_workflow_state(
            _issue(),
            _run(),
            _best_pr(pr_base="main"),
        )
        assert result["lane"] == LANE_PR_OPEN
        assert any("wrong_base" in w for w in result["warnings"])

    def test_merged_pr_is_done(self) -> None:
        """Acceptance criterion 5: PR merges → lane done."""
        result = compute_workflow_state(
            _issue(),
            _run(status="completed", agent_status="completed"),
            _best_pr(pr_state="merged"),
        )
        assert result["lane"] == LANE_DONE

    def test_merged_pr_stabilisation_prevents_flicker(self) -> None:
        """Acceptance criterion 5: issue still open after merge → done + warning."""
        result = compute_workflow_state(
            _issue(state="open"),
            _run(status="completed", agent_status="completed"),
            _best_pr(pr_state="merged"),
            pr_merged_recently=True,
        )
        assert result["lane"] == LANE_DONE
        assert "issue_close_pending_propagation" in result["warnings"]
        assert result["issue_state"] == "close_pending"

    def test_reviewing_pr_is_reviewing(self) -> None:
        result = compute_workflow_state(
            _issue(),
            _run(agent_status="reviewing"),
            _best_pr(),
        )
        assert result["lane"] == LANE_REVIEWING

    def test_active_run_no_pr_is_active(self) -> None:
        result = compute_workflow_state(
            _issue(),
            _run(agent_status="implementing"),
            None,
        )
        assert result["lane"] == LANE_ACTIVE

    def test_pending_launch_is_active(self) -> None:
        result = compute_workflow_state(
            _issue(),
            _run(agent_status="pending_launch"),
            None,
        )
        assert result["lane"] == LANE_ACTIVE

    def test_blocked_run_is_active(self) -> None:
        result = compute_workflow_state(
            _issue(),
            _run(agent_status="blocked"),
            None,
        )
        assert result["lane"] == LANE_ACTIVE

    def test_no_run_no_pr_is_todo(self) -> None:
        result = compute_workflow_state(_issue(), None, None)
        assert result["lane"] == LANE_TODO

    def test_failed_run_no_pr_is_todo(self) -> None:
        result = compute_workflow_state(
            _issue(),
            _run(agent_status="failed"),
            None,
        )
        assert result["lane"] == LANE_TODO

    def test_completed_run_no_pr_is_todo(self) -> None:
        """Run completed but no PR — goes back to todo (work might have failed silently)."""
        result = compute_workflow_state(
            _issue(),
            _run(agent_status="completed"),
            None,
        )
        assert result["lane"] == LANE_TODO


class TestRunPRMismatchWarning:
    """Acceptance criterion 3: Run has pr_number but PR not in links."""

    def test_run_claims_pr_no_link_produces_warning(self) -> None:
        result = compute_workflow_state(
            _issue(),
            _run(pr_number=42),
            None,
        )
        assert any("run_claims_pr_missing_from_links" in w for w in result["warnings"])

    def test_run_claims_pr_with_link_no_warning(self) -> None:
        result = compute_workflow_state(
            _issue(),
            _run(pr_number=42),
            _best_pr(pr_number=42),
        )
        assert not any("run_claims_pr_missing" in w for w in result["warnings"])


class TestContentHash:
    """State hash prevents no-op writes."""

    def test_identical_inputs_produce_same_hash(self) -> None:
        r1 = compute_workflow_state(_issue(), _run(), _best_pr())
        r2 = compute_workflow_state(_issue(), _run(), _best_pr())
        assert r1["content_hash"] == r2["content_hash"]

    def test_different_lane_produces_different_hash(self) -> None:
        r1 = compute_workflow_state(_issue(), _run(), _best_pr())
        r2 = compute_workflow_state(_issue(state="closed"), _run(), _best_pr())
        assert r1["content_hash"] != r2["content_hash"]


# ===========================================================================
# 2) PR↔Issue Linker
# ===========================================================================


class TestLinkDiscovery:
    """Linker discovers all signal types with correct confidence."""

    def test_body_closes_reference(self) -> None:
        pr = _pr_row(body="This PR:\n\nCloses #17\n\nDone.")
        links = discover_links_for_pr(pr, _REPO)
        body_links = [l for l in links if l["link_method"] == "body_closes"]
        assert len(body_links) == 1
        assert body_links[0]["issue_number"] == 17
        assert body_links[0]["confidence"] == 95

    def test_body_fixes_reference(self) -> None:
        pr = _pr_row(body="Fixes #42")
        links = discover_links_for_pr(pr, _REPO)
        body_links = [l for l in links if l["link_method"] == "body_closes"]
        assert len(body_links) == 1
        assert body_links[0]["issue_number"] == 42

    def test_body_resolves_reference(self) -> None:
        pr = _pr_row(body="resolves #99")
        links = discover_links_for_pr(pr, _REPO)
        body_links = [l for l in links if l["link_method"] == "body_closes"]
        assert body_links[0]["issue_number"] == 99

    def test_body_multiple_closes(self) -> None:
        pr = _pr_row(body="Closes #17\nFixes #18\nResolves #19")
        links = discover_links_for_pr(pr, _REPO)
        body_links = [l for l in links if l["link_method"] == "body_closes"]
        nums = {l["issue_number"] for l in body_links}
        assert nums == {17, 18, 19}

    def test_branch_regex(self) -> None:
        """ac/issue-{N} branch convention is detected as branch_regex at confidence 90."""
        pr = _pr_row(head_ref="ac/issue-17")
        links = discover_links_for_pr(pr, _REPO)
        branch_links = [l for l in links if l["link_method"] == "branch_regex"]
        assert len(branch_links) == 1
        assert branch_links[0]["issue_number"] == 17
        assert branch_links[0]["confidence"] == 90

    def test_old_feat_branch_not_matched(self) -> None:
        """Legacy feat/issue-{N}-slug branches are no longer matched."""
        pr = _pr_row(head_ref="feat/issue-17-fix-buttons")
        links = discover_links_for_pr(pr, _REPO)
        branch_links = [l for l in links if l["link_method"] == "branch_regex"]
        assert branch_links == []

    def test_run_pr_number(self) -> None:
        pr = _pr_row(number=42)
        runs_by_pr = {42: [RunRow(id="issue-17", issue_number=17, pr_number=42)]}
        links = discover_links_for_pr(pr, _REPO, runs_by_pr)
        run_links = [l for l in links if l["link_method"] == "run_pr_number"]
        assert len(run_links) == 1
        assert run_links[0]["issue_number"] == 17
        assert run_links[0]["confidence"] == 85

    def test_no_signals_empty(self) -> None:
        pr = _pr_row(head_ref="main", body="No references here")
        links = discover_links_for_pr(pr, _REPO)
        assert links == []

    def test_sorted_by_confidence_desc(self) -> None:
        """Results from multiple signals are sorted highest confidence first."""
        pr = _pr_row(
            head_ref="ac/issue-17",
            body="Closes #17",
        )
        links = discover_links_for_pr(pr, _REPO)
        confidences = [l["confidence"] for l in links]
        assert confidences == sorted(confidences, reverse=True)


class TestBestPRSelection:
    """Best-PR selection precedence rules."""

    def test_prefers_open_over_merged(self) -> None:
        links = [
            CandidateLink(repo=_REPO, pr_number=1, issue_number=17, link_method="body_closes", confidence=95, evidence_json="{}"),
            CandidateLink(repo=_REPO, pr_number=2, issue_number=17, link_method="body_closes", confidence=95, evidence_json="{}"),
        ]
        pr_info = {
            1: PRInfo(number=1, state="merged", base_ref="dev", head_ref="ac/issue-17"),
            2: PRInfo(number=2, state="open", base_ref="dev", head_ref="ac/issue-17-b"),
        }
        best = best_pr_for_issue(17, links, pr_info)
        assert best is not None
        assert best["pr_number"] == 2
        assert best["pr_state"] == "open"

    def test_prefers_higher_confidence(self) -> None:
        links = [
            CandidateLink(repo=_REPO, pr_number=1, issue_number=17, link_method="title_mention", confidence=60, evidence_json="{}"),
            CandidateLink(repo=_REPO, pr_number=2, issue_number=17, link_method="body_closes", confidence=95, evidence_json="{}"),
        ]
        pr_info = {
            1: PRInfo(number=1, state="open", base_ref="dev", head_ref="fix/a"),
            2: PRInfo(number=2, state="open", base_ref="dev", head_ref="fix/b"),
        }
        best = best_pr_for_issue(17, links, pr_info)
        assert best is not None
        assert best["pr_number"] == 2

    def test_prefers_higher_pr_number_as_tiebreak(self) -> None:
        links = [
            CandidateLink(repo=_REPO, pr_number=1, issue_number=17, link_method="body_closes", confidence=95, evidence_json="{}"),
            CandidateLink(repo=_REPO, pr_number=5, issue_number=17, link_method="body_closes", confidence=95, evidence_json="{}"),
        ]
        pr_info = {
            1: PRInfo(number=1, state="open", base_ref="dev", head_ref="fix/a"),
            5: PRInfo(number=5, state="open", base_ref="dev", head_ref="fix/b"),
        }
        best = best_pr_for_issue(17, links, pr_info)
        assert best is not None
        assert best["pr_number"] == 5

    def test_no_links_returns_none(self) -> None:
        assert best_pr_for_issue(17, [], {}) is None

    def test_other_issue_links_ignored(self) -> None:
        links = [
            CandidateLink(repo=_REPO, pr_number=1, issue_number=99, link_method="body_closes", confidence=95, evidence_json="{}"),
        ]
        pr_info = {1: PRInfo(number=1, state="open", base_ref="dev", head_ref="fix/a")}
        assert best_pr_for_issue(17, links, pr_info) is None


# ===========================================================================
# 3) Status Module
# ===========================================================================


class TestAgentStatusEnum:
    """Canonical status sets are consistent and complete."""

    def test_active_statuses_excludes_pending_launch(self) -> None:
        assert "pending_launch" not in ACTIVE_STATUSES

    def test_live_statuses_includes_pending_launch(self) -> None:
        assert "pending_launch" in LIVE_STATUSES

    def test_lane_active_includes_all_relevant(self) -> None:
        assert "implementing" in LANE_ACTIVE_STATUSES
        assert "pending_launch" in LANE_ACTIVE_STATUSES
        assert "blocked" in LANE_ACTIVE_STATUSES
        assert "reviewing" in LANE_ACTIVE_STATUSES

    def test_reset_statuses(self) -> None:
        assert RESET_STATUSES == {
            "pending_launch", "implementing", "blocked", "reviewing",
            "stalled", "recovering",
        }

    def test_is_active(self) -> None:
        assert is_active("implementing")
        assert is_active("blocked")
        assert not is_active("pending_launch")
        assert not is_active("completed")

    def test_is_live(self) -> None:
        assert is_live("implementing")
        assert is_live("pending_launch")
        assert is_live("blocked")
        assert not is_live("completed")


class TestComputeAgentStatus:
    """Staleness logic from the canonical compute_agent_status."""

    def test_fresh_implementing_stays(self) -> None:
        import datetime
        now = datetime.datetime.now(datetime.timezone.utc)
        assert compute_agent_status("implementing", now, now=now) == "implementing"

    def test_stale_implementing_becomes_stale(self) -> None:
        import datetime
        now = datetime.datetime.now(datetime.timezone.utc)
        old = now - datetime.timedelta(hours=1)
        assert compute_agent_status("implementing", old, now=now) == "stale"

    def test_unknown_status_normalised(self) -> None:
        assert compute_agent_status("garbage_status", None) == "failed"


# ===========================================================================
# 4) Invariant Checks
# ===========================================================================


class TestInvariants:
    """Invariant check functions."""

    def _base_ctx(self) -> InvariantContext:
        return InvariantContext(
            repo=_REPO,
            issue_numbers=[17],
            pr_numbers_in_db={42},
            run_pr_numbers={},
            link_issue_numbers_by_pr={},
            workflow_states={},
            pr_states={42: "open"},
            pr_bases={42: "dev"},
            closes_refs_by_pr={},
        )

    def test_clean_context_no_alerts(self) -> None:
        alerts = check_invariants(self._base_ctx())
        assert alerts == []

    def test_inv_pr_link_1_missing_link(self) -> None:
        ctx = self._base_ctx()
        ctx["closes_refs_by_pr"] = {42: [17]}
        ctx["link_issue_numbers_by_pr"] = {}
        alerts = check_invariants(ctx)
        assert any("INV-PR-LINK-1" in a for a in alerts)

    def test_inv_pr_link_1_present_link_no_alert(self) -> None:
        ctx = self._base_ctx()
        ctx["closes_refs_by_pr"] = {42: [17]}
        ctx["link_issue_numbers_by_pr"] = {42: [17]}
        alerts = check_invariants(ctx)
        assert not any("INV-PR-LINK-1" in a for a in alerts)

    def test_inv_run_pr_1_missing_pr(self) -> None:
        ctx = self._base_ctx()
        ctx["run_pr_numbers"] = {"issue-17": 999}
        alerts = check_invariants(ctx)
        assert any("INV-RUN-PR-1" in a for a in alerts)

    def test_inv_lane_1_pr_open_no_pr(self) -> None:
        ctx = self._base_ctx()
        ctx["workflow_states"] = {
            17: WorkflowSnapshot(
                lane="pr_open",
                pr_number=None,
                pr_state=None,
                agent_status="implementing",
                issue_state="open",
            )
        }
        alerts = check_invariants(ctx)
        assert any("INV-LANE-1" in a for a in alerts)

    def test_inv_base_1_wrong_base(self) -> None:
        ctx = self._base_ctx()
        ctx["pr_bases"] = {42: "main"}
        ctx["pr_states"] = {42: "open"}
        alerts = check_invariants(ctx)
        assert any("INV-BASE-1" in a for a in alerts)


# ===========================================================================
# 5) Integration: End-to-end lane derivation
# ===========================================================================


class TestEndToEndLaneDerivation:
    """Simulate the full pipeline: discover links → best PR → compute lane."""

    def test_closes_body_drives_pr_open(self) -> None:
        """Acceptance criterion 1: any branch + Closes #17 → pr_open."""
        pr = _pr_row(number=42, head_ref="fix/my-random-branch", body="Closes #17")
        links = discover_links_for_pr(pr, _REPO)
        pr_info = {42: PRInfo(number=42, state="open", base_ref="dev", head_ref="fix/my-random-branch")}
        best = best_pr_for_issue(17, links, pr_info)
        result = compute_workflow_state(_issue(number=17), _run(), best)
        assert result["lane"] == LANE_PR_OPEN

    def test_branch_convention_drives_pr_open(self) -> None:
        """Acceptance criterion 2: ac/issue-17 branch → pr_open."""
        pr = _pr_row(number=42, head_ref="ac/issue-17", body="")
        links = discover_links_for_pr(pr, _REPO)
        pr_info = {42: PRInfo(number=42, state="open", base_ref="dev", head_ref="ac/issue-17")}
        best = best_pr_for_issue(17, links, pr_info)
        result = compute_workflow_state(_issue(number=17), _run(), best)
        assert result["lane"] == LANE_PR_OPEN

    def test_run_pr_number_drives_pr_open(self) -> None:
        """Acceptance criterion 3: agent reports pr_number → pr_open."""
        pr = _pr_row(number=42, head_ref="random/branch", body="No closes ref")
        runs_by_pr = {42: [RunRow(id="issue-17", issue_number=17, pr_number=42)]}
        links = discover_links_for_pr(pr, _REPO, runs_by_pr)
        pr_info = {42: PRInfo(number=42, state="open", base_ref="dev", head_ref="random/branch")}
        best = best_pr_for_issue(17, links, pr_info)
        result = compute_workflow_state(_issue(number=17), _run(pr_number=42), best)
        assert result["lane"] == LANE_PR_OPEN

    def test_wrong_base_still_pr_open(self) -> None:
        """Acceptance criterion 4: PR against main → pr_open + warning."""
        pr = _pr_row(number=42, head_ref="ac/issue-17", body="Closes #17", base_ref="main")
        links = discover_links_for_pr(pr, _REPO)
        pr_info = {42: PRInfo(number=42, state="open", base_ref="main", head_ref="ac/issue-17")}
        best = best_pr_for_issue(17, links, pr_info)
        result = compute_workflow_state(_issue(number=17), _run(), best)
        assert result["lane"] == LANE_PR_OPEN
        assert any("wrong_base" in w for w in result["warnings"])

    def test_merge_to_done_stabilised(self) -> None:
        """Acceptance criterion 5: merge → done even if issue still open."""
        result = compute_workflow_state(
            _issue(number=17, state="open"),
            _run(status="completed", agent_status="completed"),
            _best_pr(pr_state="merged"),
            pr_merged_recently=True,
        )
        assert result["lane"] == LANE_DONE
        assert "issue_close_pending_propagation" in result["warnings"]

    def test_multiple_runs_latest_failed_older_has_pr(self) -> None:
        """Acceptance criterion 6: latest run failed, older run produced PR."""
        # The state machine gets the best PR (from links), not from the run.
        # Even if the latest run failed, if a PR link exists, lane is pr_open.
        result = compute_workflow_state(
            _issue(number=17),
            _run(agent_status="failed"),   # latest run failed
            _best_pr(pr_state="open"),     # but PR link exists from older run
        )
        assert result["lane"] == LANE_PR_OPEN
