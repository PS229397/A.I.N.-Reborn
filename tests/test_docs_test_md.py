from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
DOCS_DIR = REPO_ROOT / "docs"
DOCS_TEST_MD = DOCS_DIR / "test.md"
README_FILE = REPO_ROOT / "README.md"
NAV_CANDIDATES = [
    README_FILE,
    DOCS_DIR / "index.md",
    DOCS_DIR / "README.md",
    DOCS_DIR / "_sidebar.md",
    DOCS_DIR / "_navbar.md",
    DOCS_DIR / "SUMMARY.md",
]
EXPECTED_CONTENT = "# test"


def test_docs_test_md_matches_sentinel_spec():
    assert DOCS_TEST_MD.exists(), "docs/test.md should exist in the docs directory"

    content = DOCS_TEST_MD.read_text(encoding="utf-8")
    lines = content.splitlines()

    assert len(lines) == 1, "docs/test.md must contain exactly one line"
    assert lines[0] == EXPECTED_CONTENT, "docs/test.md must contain only the single heading '# test'"
    assert content in {EXPECTED_CONTENT, f"{EXPECTED_CONTENT}\n"}, "docs/test.md must not include additional content or whitespace"


def test_docs_test_md_not_linked_from_readme_or_nav():
    link_tokens = {
        "docs/test.md",
        "(docs/test.md",
        "(./test.md",
        "(test.md",
        "[test.md",
    }

    for nav_file in NAV_CANDIDATES:
        if not nav_file.exists():
            continue

        text = nav_file.read_text(encoding="utf-8")
        assert not any(token in text for token in link_tokens), f"{nav_file.relative_to(REPO_ROOT)} should not link to docs/test.md"
