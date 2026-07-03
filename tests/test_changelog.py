import re
from datetime import datetime
from pathlib import Path

CHANGELOG_PATH = Path("CHANGELOG.md")
CHANGELOG_HEADER_PATTERN = re.compile(
    r"^## (?P<version>\d+\.\d+\.\d+) - (?P<date>Unreleased|\d{2}\.\d{2}\.\d{4})$"
)


def test_changelog_version_headers_use_unbracketed_versions_and_requested_dates() -> None:
    headers = [
        line.strip()
        for line in CHANGELOG_PATH.read_text(encoding="utf-8").splitlines()
        if line.startswith("## ")
    ]

    assert headers, "CHANGELOG.md should contain version headers"
    for header in headers:
        match = CHANGELOG_HEADER_PATTERN.match(header)
        assert match is not None, f"invalid changelog header format: {header}"
        header_date = match.group("date")
        if header_date != "Unreleased":
            parsed_date = datetime.strptime(header_date, "%m.%d.%Y").date()
            assert parsed_date.strftime("%m.%d.%Y") == header_date
