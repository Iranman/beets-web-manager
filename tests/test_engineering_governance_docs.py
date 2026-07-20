import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]


def read_repo_file(relative_path: str) -> str:
    return (REPO_ROOT / relative_path).read_text(encoding="utf-8")


class EngineeringGovernanceDocsTest(unittest.TestCase):
    def test_required_governance_files_exist(self) -> None:
        required_paths = [
            "AGENTS.md",
            "CLAUDE.md",
            "REVIEW.md",
            "docs/AI_ENGINEERING_RULES.md",
            "docs/ARCHITECTURE.md",
            "docs/TECHNICAL_DEBT.md",
            "docs/operations/DEVELOPMENT_AND_DEPLOYMENT.md",
            "docs/incidents/LEGACY_AGENT_FILE_RECOVERY.md",
        ]

        for relative_path in required_paths:
            with self.subTest(path=relative_path):
                self.assertTrue((REPO_ROOT / relative_path).is_file())

    def test_agent_files_point_to_shared_source_of_truth(self) -> None:
        required_references = [
            "docs/AI_ENGINEERING_RULES.md",
            "docs/ARCHITECTURE.md",
            "docs/TECHNICAL_DEBT.md",
            "REVIEW.md",
        ]

        for agent_file in ("AGENTS.md", "CLAUDE.md"):
            content = read_repo_file(agent_file)
            for reference in required_references:
                with self.subTest(agent_file=agent_file, reference=reference):
                    self.assertIn(reference, content)

    def test_agent_files_do_not_contain_regression_markers(self) -> None:
        for agent_file in ("AGENTS.md", "CLAUDE.md"):
            with self.subTest(agent_file=agent_file):
                self.assertNotIn("Regression Markers", read_repo_file(agent_file))

    def test_shared_rules_record_non_negotiable_product_and_safety_rules(self) -> None:
        content = read_repo_file("docs/AI_ENGINEERING_RULES.md")
        expected_rules = [
            "Beets remains",
            "MusicBrainz and AcoustID are the primary identity evidence",
            "AI is optional and untrusted",
            "release-group ID",
            "Never silently modify the music library",
            "Persistent status",
            "Never expose credentials",
        ]

        for rule in expected_rules:
            with self.subTest(rule=rule):
                self.assertIn(rule, content)

    def test_architecture_doc_is_evidence_based_and_marks_incomplete_migration(self) -> None:
        content = read_repo_file("docs/ARCHITECTURE.md")
        expected_evidence = [
            "Current migration status: incomplete",
            "app.py",
            "job_engine.py",
            "helpers_mb.py",
            "frontend/src/api/client.ts",
        ]

        for evidence in expected_evidence:
            with self.subTest(evidence=evidence):
                self.assertIn(evidence, content)

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

        for debt_id in [f"ARCH-{index:03d}" for index in range(1, 10)]:
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

        arch_009 = content[content.index("ARCH-009") :]
        for phrase in (
            "release-group identity",
            "mb_albumid",
            "mb_releasegroupid",
            "Diagnostic snapshot",
            "Required tests:",
        ):
            with self.subTest(phrase=phrase):
                self.assertIn(phrase, arch_009)

    def test_initial_adrs_exist_and_have_required_sections(self) -> None:
        adr_paths = sorted((REPO_ROOT / "docs" / "adr").glob("*.md"))
        self.assertGreaterEqual(len(adr_paths), 5)

        for adr_path in adr_paths:
            content = adr_path.read_text(encoding="utf-8")
            for heading in ("Status", "Decision", "Consequences"):
                with self.subTest(adr=adr_path.name, heading=heading):
                    self.assertIn(heading, content)

    def test_backup_files_are_ignored_narrowly(self) -> None:
        gitignore = read_repo_file(".gitignore")
        self.assertIn("AGENTS.md.bak-*", gitignore)
        self.assertIn("CLAUDE.md.bak-*", gitignore)
        self.assertIn("!docs/AI_ENGINEERING_RULES.md", gitignore)
        self.assertIn("!docs/operations/*.md", gitignore)
        self.assertIn("!docs/incidents/*.md", gitignore)


if __name__ == "__main__":
    unittest.main()
