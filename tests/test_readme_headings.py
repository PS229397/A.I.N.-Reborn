from pathlib import Path


README_FILE = Path(__file__).resolve().parents[1] / "README.md"


def test_readme_has_single_h1_and_expected_leading_headings():
    lines = README_FILE.read_text(encoding="utf-8").splitlines()
    heading_lines = []
    in_fenced_block = False

    for line in lines:
        if line.startswith("```"):
            in_fenced_block = not in_fenced_block
            continue
        if in_fenced_block:
            continue
        if line.startswith("#"):
            heading_lines.append(line)

    assert heading_lines[:2] == ["# A.I.N. Pipeline", "## Installation"]
    assert sum(1 for line in heading_lines if line.startswith("# ")) == 1
