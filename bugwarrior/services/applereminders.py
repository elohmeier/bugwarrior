"""One-way Apple Reminders import through a read-only EventKit helper."""

from collections.abc import Iterator
from datetime import UTC, datetime
import json
import os
from pathlib import Path
import platform
import subprocess
import sys
import tempfile
from typing import Annotated, Any, Literal, Self

import click
from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    ValidationError,
    model_validator,
)

from bugwarrior import config
from bugwarrior.services import Issue, Service

DEFAULT_HELPER = "~/.local/libexec/bugwarrior-reminders"
Identifier = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]


class AppleRemindersConfig(config.ServiceConfig):
    service: Literal["applereminders"]
    KEYRING_SERVICE = "applereminders://{namespace}"

    helper_path: config.ExpandedPath = Path(DEFAULT_HELPER)
    namespace: Identifier = "apple-reminders"
    list_ids: config.ConfigList = []
    include_completed: bool = True
    timeout: int = Field(default=60, gt=0)
    default_priority: config.Priority = ""

    only_if_assigned: config.UnsupportedOption[str] = ""
    also_unassigned: config.UnsupportedOption[bool] = False


class SnapshotModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class ReminderDate(SnapshotModel):
    value: AwareDatetime
    date_only: bool
    time_zone: str | None
    local_date: str


class ReminderList(SnapshotModel):
    id: Identifier
    title: str
    source_id: Identifier
    source_title: str


class Reminder(SnapshotModel):
    id: Identifier
    local_id: Identifier
    id_kind: Literal["external", "local"]
    calendar_id: Identifier
    title: str
    notes: str | None
    url: str | None
    created: AwareDatetime | None
    modified: AwareDatetime | None
    completed: bool
    completed_at: AwareDatetime | None
    due: ReminderDate | None
    start: ReminderDate | None
    priority: int = Field(ge=0, le=9)
    recurrence: list[dict[str, Any]]
    alarms: list[dict[str, Any]]


class ReminderSnapshot(SnapshotModel):
    version: Literal[1]
    calendars: list[ReminderList]
    reminders: list[Reminder]

    @model_validator(mode="after")
    def validate_identity(self) -> Self:
        calendar_ids = [calendar.id for calendar in self.calendars]
        reminder_ids = [reminder.id for reminder in self.reminders]
        local_ids = [reminder.local_id for reminder in self.reminders]
        if len(set(calendar_ids)) != len(calendar_ids):
            raise ValueError("Duplicate reminder list identifiers")
        if len(set(reminder_ids)) != len(reminder_ids):
            raise ValueError(
                "Duplicate reminder identifiers; use separate source namespaces"
            )
        if len(set(local_ids)) != len(local_ids):
            raise ValueError("Duplicate local reminder identifiers")
        if any(r.calendar_id not in calendar_ids for r in self.reminders):
            raise ValueError("A reminder belongs to an unavailable list")
        return self


class AppleRemindersClient:
    def __init__(self, helper_path: Path, timeout: int) -> None:
        self.helper_path = helper_path
        self.timeout = timeout

    def get_snapshot(self) -> ReminderSnapshot:
        if sys.platform != "darwin":
            raise RuntimeError("Apple Reminders collection requires macOS 14 or later")
        try:
            result = subprocess.run(
                [str(self.helper_path), "snapshot"],
                capture_output=True,
                text=True,
                check=True,
                timeout=self.timeout,
            )
        except FileNotFoundError:
            raise RuntimeError(
                "Reminders helper not found; run bugwarrior-reminders-helper, "
                "then authorize the helper before importing"
            ) from None
        except subprocess.TimeoutExpired:
            raise RuntimeError(
                "Reminders snapshot timed out; no tasks will be imported"
            ) from None
        except subprocess.CalledProcessError:
            # Avoid logging a helper's potentially sensitive stdout/stderr.
            raise RuntimeError(
                "Reminders helper failed; check permission with the helper's "
                "authorize command in the same execution context"
            ) from None
        try:
            return ReminderSnapshot.model_validate_json(result.stdout)
        except ValidationError:
            raise RuntimeError(
                "Invalid Reminders snapshot; check the helper version"
            ) from None


def utc_seconds(value: datetime | None) -> datetime | None:
    return value.astimezone(UTC).replace(microsecond=0) if value else None


class AppleRemindersIssue(Issue):
    NAMESPACE = "appleremindersnamespace"
    ID = "appleremindersid"
    LOCAL_ID = "applereminderslocalid"
    LIST_ID = "applereminderslistid"
    LIST = "applereminderslist"
    TITLE = "applereminderstitle"
    URL = "appleremindersurl"
    MODIFIED = "appleremindersmodified"
    METADATA = "appleremindersmetadata"

    UNIQUE_KEY = (NAMESPACE, ID)
    UDAS = {
        NAMESPACE: {"type": "string", "label": "Reminders Namespace"},
        ID: {"type": "string", "label": "Reminder ID"},
        LOCAL_ID: {"type": "string", "label": "Reminder Local ID"},
        LIST_ID: {"type": "string", "label": "Reminders List ID"},
        LIST: {"type": "string", "label": "Reminders List"},
        TITLE: {"type": "string", "label": "Reminder Title"},
        URL: {"type": "string", "label": "Reminder URL"},
        MODIFIED: {"type": "date", "label": "Reminder Modified"},
        METADATA: {"type": "string", "label": "Reminder Source Metadata"},
    }
    PRIORITY_MAP: dict[int, config.Priority] = {
        0: "",
        1: "H",
        2: "H",
        3: "H",
        4: "H",
        5: "M",
        6: "L",
        7: "L",
        8: "L",
        9: "L",
    }

    def get_default_description(self) -> str:
        # Titles and notes are archival data; do not apply the issue tracker's
        # default truncation or synthesize unsupported Reminders deep links.
        return self.record["title"].strip() or "(Untitled reminder)"

    def to_taskwarrior(self) -> dict[str, Any]:
        record = self.record
        source_list = self.extra["calendar"]
        metadata = {
            key: record[key]
            for key in (
                "id_kind",
                "completed_at",
                "due",
                "start",
                "recurrence",
                "alarms",
            )
        }
        metadata.update(
            source_id=source_list["source_id"], source_title=source_list["source_title"]
        )
        task = {
            "project": source_list["title"],
            "priority": self.get_priority(),
            "annotations": [record["notes"]] if record["notes"] else [],
            "tags": [],
            "entry": utc_seconds(record["created"]),
            "due": utc_seconds(record["due"]["value"]) if record["due"] else None,
            "scheduled": utc_seconds(record["start"]["value"])
            if record["start"]
            else None,
            "status": "completed" if record["completed"] else "pending",
            self.NAMESPACE: self.config.namespace,
            self.ID: record["id"],
            self.LOCAL_ID: record["local_id"],
            self.LIST_ID: source_list["id"],
            self.LIST: source_list["title"],
            self.TITLE: record["title"],
            self.URL: record["url"],
            self.MODIFIED: utc_seconds(record["modified"]),
            self.METADATA: json.dumps(
                metadata, default=self._json_date, sort_keys=True
            ),
        }
        # Some clients mark reminders completed without a completion date.
        # Keep Taskwarrior's existing end in that case instead of resetting it
        # on each pull; retain the unknown source date in metadata.
        if not record["completed"] or record["completed_at"]:
            task["end"] = (
                utc_seconds(record["completed_at"]) if record["completed"] else None
            )
        return task

    @staticmethod
    def _json_date(value: datetime) -> str:
        return value.isoformat()


class AppleRemindersService(Service[AppleRemindersIssue]):
    API_VERSION = 2.1
    ISSUE_CLASS = AppleRemindersIssue
    CONFIG_SCHEMA = AppleRemindersConfig
    COMPLETE_MISSING = False

    def __init__(
        self, config: AppleRemindersConfig, main_config: config.MainSectionConfig
    ) -> None:
        super().__init__(config, main_config)
        self.client = AppleRemindersClient(self.config.helper_path, self.config.timeout)

    def issues(self) -> Iterator[AppleRemindersIssue]:
        # Validate the entire snapshot before yielding any records, including
        # records in excluded lists. Never accept a partial helper response.
        snapshot = self.client.get_snapshot()
        calendars = {calendar.id: calendar for calendar in snapshot.calendars}
        if set(self.config.list_ids) - calendars.keys():
            raise RuntimeError(
                "A configured Reminders list is unavailable; check list_ids"
            )
        for reminder in snapshot.reminders:
            if (
                self.config.list_ids
                and reminder.calendar_id not in self.config.list_ids
            ):
                continue
            if reminder.completed and not self.config.include_completed:
                continue
            yield self.get_issue_for_record(
                reminder.model_dump(),
                {"calendar": calendars[reminder.calendar_id].model_dump()},
            )


@click.command()
@click.option(
    "--output",
    type=click.Path(path_type=Path),
    default=DEFAULT_HELPER,
    show_default=True,
)
def build_helper(output: Path) -> None:
    """Build the read-only macOS EventKit helper (requires Xcode command line tools)."""
    if sys.platform != "darwin":
        raise click.ClickException(
            "Building the Reminders helper requires macOS 14 or later"
        )
    output = output.expanduser().absolute()
    output.parent.mkdir(parents=True, exist_ok=True)
    resources = Path(__file__).with_name("_applereminders")
    with tempfile.TemporaryDirectory(
        dir=output.parent, prefix=".reminders-build-"
    ) as temporary:
        binary = Path(temporary) / "bugwarrior-reminders"
        try:
            subprocess.run(
                [
                    "xcrun",
                    "swiftc",
                    "-swift-version",
                    "5",
                    "-target",
                    f"{platform.machine()}-apple-macos14.0",
                    str(resources / "reminders.swift"),
                    "-o",
                    str(binary),
                    "-Xlinker",
                    "-sectcreate",
                    "-Xlinker",
                    "__TEXT",
                    "-Xlinker",
                    "__info_plist",
                    "-Xlinker",
                    str(resources / "Info.plist"),
                ],
                check=True,
                capture_output=True,
                text=True,
                timeout=120,
            )
        except (OSError, subprocess.SubprocessError) as error:
            raise click.ClickException(
                "Could not build the helper; install Xcode command line tools "
                "(xcode-select --install)"
            ) from error
        os.replace(binary, output)
    click.echo(f"Built {output}\nRun '{output} authorize' before the first import.")


if __name__ == "__main__":
    build_helper()
