import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]


def read_repo_file(relative_path: str) -> str:
    return (REPO_ROOT / relative_path).read_text(encoding="utf-8")


class EngineeringGovernanceDocsTest(unittest.TestCase):
    """Structural checks on the repository's canonical documentation set.

    Scoped to product/architecture documentation only -- these assert that
    the documents a contributor actually needs exist and stay internally
    consistent, not that any particular AI-agent workflow prose is present.
    """

    def test_required_documentation_exists(self) -> None:
        required_paths = [
            "REVIEW.md",
            "CONTRIBUTING.md",
            "SECURITY.md",
            "docs/ARCHITECTURE.md",
            "docs/DEVELOPMENT.md",
            "docs/TECHNICAL_DEBT.md",
            "docs/CONFIGURATION.md",
            "docs/INSTALLATION.md",
            "docs/TROUBLESHOOTING.md",
        ]

        for relative_path in required_paths:
            with self.subTest(path=relative_path):
                self.assertTrue((REPO_ROOT / relative_path).is_file())

    def test_no_agent_convenience_files_at_repo_root(self) -> None:
        """AGENTS.md/CLAUDE.md were deliberately removed: neither provided
        any information a human contributor could not get from README.md's
        "Documentation" section plus CONTRIBUTING.md/docs/DEVELOPMENT.md.
        Being the filename a specific coding-agent tool looks for is not
        sufficient justification to reintroduce either one -- if a real
        need for either resurfaces, put the content in CONTRIBUTING.md or
        docs/DEVELOPMENT.md instead."""
        for filename in ("AGENTS.md", "CLAUDE.md"):
            with self.subTest(filename=filename):
                self.assertFalse((REPO_ROOT / filename).exists())

    def test_architecture_doc_records_non_negotiable_product_rules(self) -> None:
        content = read_repo_file("docs/ARCHITECTURE.md")
        expected_rules = [
            "Beets remains",
            "MusicBrainz and AcoustID are the primary identity evidence",
            "AI is optional and untrusted",
            "release-group ID",
            "No silent library mutations",
            "Never expose secrets",
        ]

        for rule in expected_rules:
            with self.subTest(rule=rule):
                self.assertIn(rule, content)

    def test_documented_repository_boundaries_exist(self) -> None:
        expected_paths = [
            "backend/transaction_engine.py",
            "job_engine.py",
            "routes_jobs.py",
            "routes_lidarr.py",
            "routes_setup.py",
            "routes_submissions.py",
            "frontend/src/api/client.ts",
        ]

        for relative_path in expected_paths:
            with self.subTest(path=relative_path):
                self.assertTrue((REPO_ROOT / relative_path).is_file())

        self.assertIn("class TransactionStore", read_repo_file("backend/transaction_engine.py"))
        job_engine = read_repo_file("job_engine.py")
        self.assertIn("class JobStore", job_engine)
        self.assertIn("class PythonJob", job_engine)

    def test_technical_debt_register_has_stable_ids_and_required_fields(self) -> None:
        content = read_repo_file("docs/TECHNICAL_DEBT.md")

        # Every entry currently in the lean, current-debt-only register.
        # This list is expected to change as items open/close -- update it
        # alongside the register, it is not meant to pin a fixed count.
        for debt_id in ("ARCH-001", "ARCH-002", "ARCH-004", "ARCH-005", "ARCH-006", "ARCH-009"):
            with self.subTest(debt_id=debt_id):
                self.assertIn(debt_id, content)

        required_fields = (
            "Affected area:",
            "Evidence:",
            "Current risk:",
            "Desired state:",
            "Safe migration approach:",
            "Priority:",
            "Status:",
        )
        for field in required_fields:
            with self.subTest(field=field):
                self.assertIn(field, content)

    def test_technical_debt_register_does_not_accumulate_closed_history(self) -> None:
        """Resolved debt is removed from the active register (Git history
        and the closing PR are the historical record), not kept forever as
        a "Done"/"Closed"/"Resolved" narrative entry."""
        content = read_repo_file("docs/TECHNICAL_DEBT.md")
        self.assertLess(
            len(content.splitlines()),
            400,
            "docs/TECHNICAL_DEBT.md has grown large again -- audit for "
            "resolved items that should be removed rather than accumulated.",
        )

    def test_initial_adrs_exist_and_have_required_sections(self) -> None:
        adr_paths = sorted((REPO_ROOT / "docs" / "adr").glob("*.md"))
        self.assertGreaterEqual(len(adr_paths), 5)

        for adr_path in adr_paths:
            content = adr_path.read_text(encoding="utf-8")
            for heading in ("Status", "Decision", "Consequences"):
                with self.subTest(adr=adr_path.name, heading=heading):
                    self.assertIn(heading, content)

    def test_architecture_doc_links_to_adrs(self) -> None:
        content = read_repo_file("docs/ARCHITECTURE.md")
        self.assertIn("docs/adr/", content)

    def test_no_documentation_links_to_removed_agent_process_files(self) -> None:
        """Regression guard: agent-workflow-process documents were removed
        from the product repository (see Git history for the originals).
        No remaining documentation should link to them."""
        removed_paths = (
            "AGENTS.md",
            "CLAUDE.md",
            "docs/AGENT_WORKFLOW.md",
            "docs/AI_ENGINEERING_RULES.md",
            "docs/incidents/LEGACY_AGENT_FILE_RECOVERY.md",
            "docs/operations/DEVELOPMENT_AND_DEPLOYMENT.md",
            "docs/security/codeql_repository_closure.md",
            "SECURITY_AUDIT.md",
            "security_best_practices_report.md",
        )
        doc_files = [
            p for p in REPO_ROOT.rglob("*.md")
            if "node_modules" not in p.parts and ".git" not in p.parts
        ]
        for doc_path in doc_files:
            content = doc_path.read_text(encoding="utf-8")
            for removed in removed_paths:
                with self.subTest(doc=str(doc_path.relative_to(REPO_ROOT)), removed=removed):
                    # CHANGELOG.md is a historical record and may legitimately
                    # mention a since-removed file in a past-dated entry.
                    if doc_path.name == "CHANGELOG.md":
                        continue
                    self.assertNotIn(removed, content)


if __name__ == "__main__":
    unittest.main()
