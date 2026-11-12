"""Compile the actual Swift date serializer without accessing Reminders."""

import json
from pathlib import Path
import shutil
import subprocess
import sys

import pytest


@pytest.mark.skipif(
    sys.platform != "darwin" or shutil.which("xcrun") is None,
    reason="Swift/EventKit requires macOS command line tools",
)
def test_native_dates_preserve_timezone_and_dst(tmp_path):
    source = (
        Path(__file__).parents[1]
        / "bugwarrior/services/_applereminders/reminders.swift"
    ).read_text()
    functions = source.split("// MARK: Command entry point")[0]
    harness = r"""
var dates: [Any] = []
let cases = [(3, 29, -1, true), (10, 25, -1, true), (3, 29, -1, false), (3, 29, 10, true)]
for (month, day, hour, due) in cases {
    var parts = DateComponents()
    parts.year = 2026
    parts.month = month
    parts.day = day
    parts.timeZone = TimeZone(identifier: "Europe/Berlin")!
    if hour >= 0 { parts.hour = hour; parts.minute = 30 }
    dates.append(try dateMetadata(parts, endOfDay: due))
}
try writeJSON(dates)
"""
    swift = tmp_path / "dates.swift"
    swift.write_text(functions + harness)
    executable = tmp_path / "dates"
    subprocess.run(
        ["xcrun", "swiftc", "-swift-version", "5", str(swift), "-o", str(executable)],
        capture_output=True,
        text=True,
        check=True,
        timeout=120,
    )
    result = subprocess.run(
        [str(executable)], capture_output=True, text=True, check=True, timeout=10
    )
    dates = json.loads(result.stdout)
    assert [date["value"] for date in dates] == [
        "2026-03-29T21:59:59Z",  # 23-hour day, local 23:59:59
        "2026-10-25T22:59:59Z",  # 25-hour day, local 23:59:59
        "2026-03-28T23:00:00Z",  # date-only start, local midnight
        "2026-03-29T08:30:00Z",  # timed deadline
    ]
    assert [date["date_only"] for date in dates] == [True, True, True, False]
    assert dates[0]["local_date"] == "2026-03-29"
    assert dates[0]["time_zone"] == "Europe/Berlin"
