import copy
from datetime import UTC, datetime
import json
from pathlib import Path
import subprocess
from unittest import mock

from click.testing import CliRunner
import pytest

from bugwarrior import db
from bugwarrior.collect import TaskConstructor
from bugwarrior.config.validation import Config
from bugwarrior.services import applereminders as reminders

from ..base import register_services

SERVICE_CLASS = reminders.AppleRemindersService
SERVICE_CONFIG = {"service": "applereminders", "namespace": "test-reminders"}


@pytest.fixture
def record():
    return {
        "id": "external:reminder-1",
        "local_id": "local-1",
        "id_kind": "external",
        "calendar_id": "list-1",
        "title": 'A complete title longer than thirty-five characters, with "quotes" ☃',
        "notes": "First line, with [brackets].\nSecond line, longer than forty-five characters.",
        "url": "https://example.com/reminder",
        "created": "2020-01-02T10:30:00Z",
        "modified": "2021-02-03T12:45:00Z",
        "completed": False,
        "completed_at": None,
        "due": {
            "value": "2026-03-29T21:59:59Z",
            "date_only": True,
            "time_zone": "Europe/Berlin",
            "local_date": "2026-03-29",
        },
        "start": None,
        "priority": 0,
        "recurrence": [{"frequency": 1, "interval": 2}],
        "alarms": [{"relative_offset": -900}],
    }


@pytest.fixture
def payload(record):
    return {
        "version": 1,
        "calendars": [
            {
                "id": "list-1",
                "title": "Inbox",
                "source_id": "source-1",
                "source_title": "iCloud",
            }
        ],
        "reminders": [record],
    }


@pytest.fixture
def service(make_service, payload):
    service = make_service()
    service.client = mock.Mock(spec=reminders.AppleRemindersClient)
    service.client.get_snapshot.side_effect = lambda: (
        reminders.ReminderSnapshot.model_validate_json(json.dumps(payload))
    )
    return service


def test_to_taskwarrior(service, record):
    issue = next(service.issues())
    task = TaskConstructor(issue).get_taskwarrior_record()
    assert task["description"] == record["title"]
    assert task["annotations"] == [record["notes"]]
    assert task["priority"] is None
    assert task["project"] == "Inbox"
    assert task["entry"] == datetime(2020, 1, 2, 10, 30, tzinfo=UTC)
    assert task["due"] == datetime(2026, 3, 29, 21, 59, 59, tzinfo=UTC)
    assert task["scheduled"] is None
    assert task["status"] == "pending"
    assert task["end"] is None
    assert task["appleremindersnamespace"] == "test-reminders"
    assert task["appleremindersid"] == record["id"]
    assert task["appleremindersurl"] == record["url"]
    metadata = json.loads(task["appleremindersmetadata"])
    assert metadata["due"]["date_only"] is True
    assert metadata["recurrence"] == record["recurrence"]
    assert metadata["alarms"] == record["alarms"]
    assert "recur" not in task


def test_issues(service, payload):
    payload["reminders"][0].update(completed=True, completed_at="2021-02-03T12:45:00Z")
    task = TaskConstructor(next(service.issues())).get_taskwarrior_record()
    assert task["status"] == "completed"
    assert task["end"] == datetime(2021, 2, 3, 12, 45, tzinfo=UTC)


@pytest.mark.parametrize(
    ("priority", "expected"),
    [(0, None), (1, "H"), (4, "H"), (5, "M"), (6, "L"), (9, "L")],
)
def test_priority(service, payload, priority, expected):
    payload["reminders"][0]["priority"] = priority
    assert (
        TaskConstructor(next(service.issues())).get_taskwarrior_record()["priority"]
        == expected
    )


def test_filters_and_missing_list(service, make_service, payload):
    filtered = make_service(list_ids=["list-1"], include_completed=False)
    filtered.client = service.client
    assert len(list(filtered.issues())) == 1
    payload["reminders"][0].update(completed=True, completed_at="2021-02-03T12:45:00Z")
    assert list(filtered.issues()) == []
    missing = make_service(list_ids=["unavailable"])
    missing.client = service.client
    with pytest.raises(RuntimeError, match="list is unavailable"):
        list(missing.issues())


def test_empty_title_and_unknown_completion_date(service, payload):
    payload["reminders"][0].update(title="", completed=True)
    task = TaskConstructor(next(service.issues())).get_taskwarrior_record()
    assert task["description"] == "(Untitled reminder)"
    assert task["status"] == "completed"
    assert "end" not in task
    assert json.loads(task["appleremindersmetadata"])["completed_at"] is None


@pytest.fixture
def run_helper(monkeypatch):
    monkeypatch.setattr(reminders.sys, "platform", "darwin")
    with mock.patch.object(reminders.subprocess, "run") as run:
        yield run


def test_client_uses_json_and_argument_array(run_helper, payload):
    run_helper.return_value.stdout = json.dumps(payload)
    client = reminders.AppleRemindersClient(Path("/path with spaces/reader"), 30)
    assert len(client.get_snapshot().reminders) == 1
    run_helper.assert_called_once_with(
        ["/path with spaces/reader", "snapshot"],
        capture_output=True,
        text=True,
        check=True,
        timeout=30,
    )


@pytest.mark.parametrize(
    "error",
    [
        FileNotFoundError(),
        subprocess.TimeoutExpired("reader", 1),
        subprocess.CalledProcessError(
            3, "reader", output="private title", stderr="private note"
        ),
    ],
)
def test_client_failures_do_not_expose_data(run_helper, error):
    run_helper.side_effect = error
    with pytest.raises(RuntimeError) as raised:
        reminders.AppleRemindersClient(Path("reader"), 1).get_snapshot()
    assert "private" not in str(raised.value)


@pytest.mark.parametrize(
    "invalid",
    ["truncated", "version", "duplicate", "missing_list", "naive_date", "bad_priority"],
)
def test_invalid_snapshot_is_rejected_atomically(run_helper, payload, invalid):
    if invalid == "version":
        payload["version"] = 2
    elif invalid == "duplicate":
        payload["reminders"].append(copy.deepcopy(payload["reminders"][0]))
    elif invalid == "missing_list":
        payload["calendars"] = []
    elif invalid == "naive_date":
        payload["reminders"][0]["created"] = "2020-01-02T10:30:00"
    elif invalid == "bad_priority":
        payload["reminders"][0]["priority"] = True
    run_helper.return_value.stdout = (
        "{" if invalid == "truncated" else json.dumps(payload)
    )
    with pytest.raises(RuntimeError, match="Invalid Reminders snapshot"):
        reminders.AppleRemindersClient(Path("reader"), 1).get_snapshot()


def test_unsupported_platform(monkeypatch):
    monkeypatch.setattr(reminders.sys, "platform", "linux")
    with pytest.raises(RuntimeError, match="requires macOS"):
        reminders.AppleRemindersClient(Path("reader"), 1).get_snapshot()
    result = CliRunner().invoke(reminders.build_helper)
    assert result.exit_code == 1
    assert "requires macOS" in result.output


def test_build_helper_is_atomic(run_helper, tmp_path):
    output = tmp_path / "helper"
    output.write_bytes(b"previous binary")
    run_helper.side_effect = subprocess.CalledProcessError(1, "swiftc")
    result = CliRunner().invoke(reminders.build_helper, ["--output", str(output)])
    assert result.exit_code == 1
    assert output.read_bytes() == b"previous binary"

    def compile_helper(args, **kwargs):
        Path(args[args.index("-o") + 1]).write_bytes(b"new binary")

    run_helper.side_effect = compile_helper
    result = CliRunner().invoke(reminders.build_helper, ["--output", str(output)])
    assert result.exit_code == 0
    assert output.read_bytes() == b"new binary"


def export_tasks(config_environment):
    result = subprocess.run(
        [
            "task",
            f"rc:{config_environment.taskrc}",
            "rc.hooks=off",
            "rc.recurrence=off",
            "rc.gc=off",
            "rc.verbose=nothing",
            "rc.json.array=on",
            "export",
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    return json.loads(result.stdout)


def test_archive_roundtrip(service, payload, config_environment, caplog):
    """Exercise the actual writer with historical, open and moved reminders."""
    record = payload["reminders"][0]
    record.update(completed=True, completed_at="2021-02-03T12:45:00Z")
    conf = Config(service_configs=[service.config], main=service.main_config)

    def sync():
        db.synchronize(
            iter(
                TaskConstructor(issue).get_data_to_sync() for issue in service.issues()
            ),
            conf,
        )

    with register_services({"applereminders": SERVICE_CLASS}):
        sync()
        first = export_tasks(config_environment)[0]
        assert first["entry"] == "20200102T103000Z"
        assert first["end"] == "20210203T124500Z"
        assert first["status"] == "completed"
        assert first["description"] == record["title"]
        assert first["applereminderstitle"] == record["title"]
        assert (
            json.loads(first["appleremindersmetadata"])["recurrence"]
            == record["recurrence"]
        )
        assert first["annotations"][0]["description"] == record["notes"]
        sync()
        assert export_tasks(config_environment) == [first]

        record.update(
            completed=False,
            completed_at=None,
            local_id="replacement-local-id",
            calendar_id="list-2",
        )
        payload["calendars"].append(
            {
                "id": "list-2",
                "title": "Renamed List",
                "source_id": "source-1",
                "source_title": "iCloud",
            }
        )
        sync()
        reopened = export_tasks(config_environment)[0]
        assert reopened["uuid"] == first["uuid"]
        assert reopened["status"] == "pending"
        assert "end" not in reopened
        assert reopened["project"] == "Renamed List"

        # Disappearing from a snapshot cannot complete or delete an archive item.
        payload["reminders"] = []
        sync()
        assert export_tasks(config_environment) == [reopened]

        record.update(completed=True, completed_at="2021-02-03T12:45:00Z")
        payload["reminders"] = [record]
        sync()
        completed = export_tasks(config_environment)[0]
        assert completed["uuid"] == first["uuid"]
        assert completed["end"] == first["end"]
        assert not [r for r in caplog.records if r.levelname == "ERROR"]


@pytest.mark.parametrize(
    "title",
    [
        '"quoted"',
        '""both""',
        '"leading',
        'trailing"',
        "literal &dquot; [brackets] \\path",
        "line\nbreak",
        "Buy 🍎 and take 🛠️ notes",
    ],
)
def test_literal_title_roundtrip(service, payload, config_environment, title):
    payload["reminders"][0]["title"] = title
    conf = Config(service_configs=[service.config], main=service.main_config)
    with register_services({"applereminders": SERVICE_CLASS}):
        for value in (title, title + " updated"):
            payload["reminders"][0]["title"] = value
            db.synchronize(
                iter(TaskConstructor(i).get_data_to_sync() for i in service.issues()),
                conf,
            )
            tasks = export_tasks(config_environment)
            assert len(tasks) == 1
            assert tasks[0]["description"] == value
            assert tasks[0]["applereminderstitle"] == value
