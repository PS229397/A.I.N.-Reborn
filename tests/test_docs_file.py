from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
DOCS_TEST_MD = REPO_ROOT / "docs" / "test.md"
EXPECTED_CONTENT = "# test"


def test_docs_test_file_contract():
    assert DOCS_TEST_MD.exists(), "docs/test.md should exist"

    content = DOCS_TEST_MD.read_text(encoding="utf-8")
    lines = content.splitlines()

    assert len(lines) == 1, "docs/test.md must contain exactly one line"
    assert lines[0] == EXPECTED_CONTENT, "docs/test.md must contain only the '# test' heading"
    assert content in {EXPECTED_CONTENT, f"{EXPECTED_CONTENT}\n"}, "docs/test.md must not include additional content or whitespace"
