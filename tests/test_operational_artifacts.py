"""Release and incident documents retain every mandatory section."""

from pathlib import Path


ROOT = Path(__file__).parents[1]


def test_all_incident_playbooks_have_required_actions() -> None:
    content = (ROOT / "docs" / "INCIDENT_PLAYBOOKS.md").read_text(encoding="utf-8")
    incidents = (
        "Private stream outage",
        "Public data outage",
        "Missing stop",
        "Partial option spread",
        "Unknown position",
        "Database outage",
        "Authentication failure",
        "Large stop slippage",
        "Expiry anomaly",
        "Liquidation-distance deterioration",
    )

    for incident in incidents:
        section = content.split(f"## {incident}", maxsplit=1)[1].split("\n## ", maxsplit=1)[0]
        for heading in (
            "### Detection",
            "### Automatic action",
            "### Manual action",
            "### Verification",
            "### Review",
        ):
            assert heading in section


def test_release_and_demo_templates_keep_external_approvals_unsigned() -> None:
    release = (ROOT / "docs" / "RELEASE_PROCESS.md").read_text(encoding="utf-8")
    demo = (ROOT / "docs" / "DEMO_BURN_IN_REVIEWS.md").read_text(encoding="utf-8")
    pilot = (ROOT / "docs" / "PILOT_APPROVAL.md").read_text(encoding="utf-8")

    for requirement in (
        "Versioned migration",
        "Changelog",
        "Configuration diff",
        "Risk review",
        "Rollback procedure",
        "Demo smoke test",
        "Shadow replay",
        "Operator approval",
    ):
        assert requirement in release
    for stage in ("D1", "D2", "D3", "D4", "D5", "D6"):
        assert f"## {stage}" in demo
    assert "Status: NOT APPROVED" in pilot
    assert "Legal/account eligibility: UNCONFIRMED" in pilot
