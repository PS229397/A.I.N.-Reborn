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
