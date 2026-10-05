import datetime
import time
from unittest import mock

import pytest

from bugwarrior import db
from bugwarrior.collect import TaskConstructor
from bugwarrior.config.validation import Config
from bugwarrior.services.logseq import LogseqClient, LogseqIssue, LogseqService

SERVICE_CLASS = LogseqService

SERVICE_CONFIG = {
    "service": "logseq",
    "host": "localhost",
    "port": 12315,
    "token": "TESTTOKEN",
}


@pytest.fixture
def local_timezone(request, monkeypatch):
    if not hasattr(time, "tzset"):
        pytest.skip("Changing the local timezone requires time.tzset")
    try:
        with monkeypatch.context() as timezone_patch:
            timezone_patch.setenv("TZ", request.param)
            time.tzset()
            yield
    finally:
        time.tzset()


@pytest.fixture
def record():
    return {
        "properties": {
            "id": "67dae9ea-8e4d-4ad1-91dc-72aacc72a802",
            "duration": '{"TODO":[0,1699562197346]}',
        },
        "priority": "C",
        "properties-order": ["duration", "id"],
        "parent": {"id": 7083},
        "id": 7146,
        "uuid": "66699a83-3ee0-4edc-81c6-a24c9b80bec6",
        "path-refs": [
            {"id": 4},
            {"id": 10},
            {"id": 555},
            {"id": 559},
            {"id": 568},
            {"id": 1777},
            {"id": 7070},
        ],
        "content": (
            "DOING [#C] Do something http://example.com/page#NotATag `#code`"
            " #[[Test tag one]] #[[TestTagTwo]] #TestTagThree\n"
            "SCHEDULED: <2025-07-01 Tue>\n"
            "DEADLINE: <2025-07-31 Thu>\n"
            "id:: 67dae9ea-8e4d-4ad1-91dc-72aacc72a802\n"
            ":LOGBOOK:\n"
            "CLOCK: [2025-06-03 Tue 13:56:47]--[2025-06-03 Tue 13:56:49] =>  00:00:02\n"
            ":END:"
        ),
        "properties-text-values": {
            "duration": '{"TODO":[0,1699562197346]}',
            "id": "67dae9ea-8e4d-4ad1-91dc-72aacc72a802",
        },
        "marker": "DOING",
        "page": {"id": 7070},
        "left": {"id": 7109},
        "format": "markdown",
        "refs": [{"id": 4}, {"id": 10}, {"id": 555}, {"id": 568}],
    }


@pytest.fixture
def extra():
    return {
        "baseURI": "logseq://graph/Test?block-id=",
        "graph": "Test",
        "page_title": "TestPageTitle",
    }


@pytest.fixture
def page():
    return {
        "updatedAt": 1751385600000,
        "journalDay": 20250701,
        "createdAt": 1751371200000,
        "id": 19,
        "name": "jul 1st, 2025",
        "uuid": "6692f0c1-f610-40e3-840f-ba763627de40",
        "journal?": True,
        "originalName": "Jul 1st, 2025",
        "file": {"id": 25},
        "format": "markdown",
    }


class TestLogseqIssue:
    @pytest.fixture
    def service(self, make_service):
        service = make_service()
        service.client = mock.MagicMock(spec=LogseqClient)
        return service

    def test_to_taskwarrior(self, service, record, extra):
        issue = service.get_issue_for_record(record, extra)

        expected = {
            "annotations": [],
            "due": datetime.datetime(year=2025, month=7, day=31).astimezone(
                datetime.UTC
            ),
            "scheduled": datetime.datetime(year=2025, month=7, day=1).astimezone(
                datetime.UTC
            ),
            "wait": None,
            "status": "pending",
            "priority": "L",
            "project": extra["graph"],
            "tags": [],
            issue.ID: str(record["id"]),
            issue.UUID: record["uuid"],
            issue.STATE: record["marker"],
            issue.TITLE: "Do something http://example.com/page#NotATag `#code`"
            + " #【Test tag one】 #【TestTagTwo】 #TestTagThree",
            issue.URI: extra["baseURI"] + record["uuid"],
            issue.SCHEDULED: datetime.datetime(year=2025, month=7, day=1).astimezone(
                datetime.UTC
            ),
            issue.DEADLINE: datetime.datetime(year=2025, month=7, day=31).astimezone(
                datetime.UTC
            ),
            issue.PAGE: "TestPageTitle",
        }

        actual = issue.to_taskwarrior()

        assert actual == expected

    @pytest.mark.parametrize("block_id", [7146, 7146.0, "7146"])
    def test_to_taskwarrior_normalizes_id(self, service, record, extra, block_id):
        # Logseq may return the block id as a float (7146.0), which would
        # otherwise be stored as the string "7146.000000" and break the
        # unique key across syncs.
        record["id"] = block_id
        issue = service.get_issue_for_record(record, extra)

        actual = issue.to_taskwarrior()

        assert actual[issue.ID] == "7146"

    @pytest.mark.parametrize("local_timezone", ["UTC", "Europe/Berlin"], indirect=True)
    @pytest.mark.parametrize(
        "content",
        [
            "DOING Unchanged task",
            "DOING Unchanged task\nSCHEDULED: <2025-10-22 Wed>",
            "DOING Unchanged task\nDEADLINE: <2025-01-22 Wed>",
            "DOING Unchanged task\nSCHEDULED: <2025-07-01 Tue 12:30 .+1d>\n"
            "DEADLINE: <2025-07-31 Thu .+1d>",
        ],
    )
    @pytest.mark.parametrize("block_id", [7146, 7146.0])
    def test_unchanged_task_is_not_updated(
        self, service, record, extra, block_id, content, local_timezone
    ):
        record.update(id=block_id, content=content)
        conf = Config(service_configs=[service.config], main=service.main_config)
        tw = db.TaskWarriorShellout(
            config_filename=conf.main.taskrc,
            config_overrides=db.build_uda_config_overrides(["logseq"]),
            marshal=True,
        )

        def synchronize():
            issue = service.get_issue_for_record(record, extra)
            collected = TaskConstructor(issue).get_data_to_sync()
            db.synchronize(iter([collected]), conf)

        with mock.patch("bugwarrior.db.TaskWarriorShellout", return_value=tw):
            synchronize()
            first_task = tw.load_tasks()["pending"][0]
            assert first_task["logseqid"] == "7146"

            with mock.patch.object(tw, "task_update", wraps=tw.task_update) as update:
                synchronize()

            update.assert_not_called()
            tasks = tw.load_tasks()["pending"]
            assert len(tasks) == 1
            assert tasks[0]["uuid"] == first_task["uuid"]

    @pytest.mark.parametrize("local_timezone", ["Europe/Berlin"], indirect=True)
    @pytest.mark.parametrize(
        ("date", "expected"),
        [
            (
                "SCHEDULED: <2025-10-22 Wed>",
                datetime.datetime(2025, 10, 21, 22, tzinfo=datetime.UTC),
            ),
            (
                "DEADLINE: <2025-01-22 Wed>",
                datetime.datetime(2025, 1, 21, 23, tzinfo=datetime.UTC),
            ),
            (
                "SCHEDULED: <2025-07-01 Tue 12:30 .+1d>",
                datetime.datetime(2025, 7, 1, 10, 30, tzinfo=datetime.UTC),
            ),
            (
                "DEADLINE: <2025-01-22 Wed 12:30>",
                datetime.datetime(2025, 1, 22, 11, 30, tzinfo=datetime.UTC),
            ),
        ],
    )
    def test_parses_local_dates_as_utc(
        self, service, record, extra, date, expected, local_timezone
    ):
        issue = service.get_issue_for_record(record, extra)

        assert issue.get_scheduled_date(date) == expected

    def test_to_taskwarrior_with_tags(self, make_service, record, extra):
        overrides = {"import_labels_as_tags": "True"}
        service = make_service(**overrides)
        issue = service.get_issue_for_record(record, extra)

        actual = issue.to_taskwarrior()
        assert actual["tags"] == ["Testtagone", "TestTagTwo", "TestTagThree"]

    def test_to_taskwarrior_todo(self, service, record, extra):
        record["content"] = "TODO test task in todo state\n"
        record["marker"] = "TODO"
        issue = service.get_issue_for_record(record, extra)
        actual = issue.to_taskwarrior()
        assert actual["status"] == "pending"

    def test_to_taskwarrior_waiting(self, service, record, extra):
        record["content"] = "WAITING test task in waiting state\n"
        record["marker"] = "WAITING"
        issue = service.get_issue_for_record(record, extra)
        actual = issue.to_taskwarrior()
        assert actual["status"] == "pending"
        assert actual["wait"] == LogseqIssue.SOMEDAY.astimezone(datetime.UTC)

    @pytest.mark.parametrize("local_timezone", ["Europe/Berlin"], indirect=True)
    @pytest.mark.parametrize(
        ("dates", "expected"),
        [
            (
                "SCHEDULED: <2025-10-22 Wed>\nDEADLINE: <2025-10-23 Thu>",
                datetime.datetime(2025, 10, 21, 22, tzinfo=datetime.UTC),
            ),
            (
                "SCHEDULED: <2025-01-23 Thu>\nDEADLINE: <2025-01-22 Wed>",
                datetime.datetime(2025, 1, 21, 23, tzinfo=datetime.UTC),
            ),
            ("", datetime.datetime(2038, 1, 17, 23, tzinfo=datetime.UTC)),
        ],
    )
    def test_waiting_uses_earliest_local_date(
        self, service, record, extra, dates, expected, local_timezone
    ):
        record.update(marker="WAITING", content=f"WAITING Unchanged task\n{dates}")
        issue = service.get_issue_for_record(record, extra)

        assert issue.to_taskwarrior()["wait"] == expected

    def test_to_taskwarrior_dates_with_time(self, service, record, extra):
        record["content"] = (
            "DOING test schedule and deadline dates with times\n"
            "SCHEDULED: <2025-07-01 Tue 12:30>\n"
            "DEADLINE: <2025-07-31 Thu 12:30>"
        )

        issue = service.get_issue_for_record(record, extra)
        actual = issue.to_taskwarrior()

        scheduled = datetime.datetime(
            year=2025, month=7, day=1, hour=12, minute=30
        ).astimezone(datetime.UTC)
        deadline = datetime.datetime(
            year=2025, month=7, day=31, hour=12, minute=30
        ).astimezone(datetime.UTC)
        assert actual["scheduled"] == scheduled
        assert actual["due"] == deadline
        assert actual[issue.SCHEDULED] == scheduled
        assert actual[issue.DEADLINE] == deadline

    def test_to_taskwarrior_dates_with_repeat(self, service, record, extra):
        record["content"] = (
            "DOING test schedule and deadline dates with times\n"
            "SCHEDULED: <2025-07-01 Tue 12:30 .+1d>\n"
            "DEADLINE: <2025-07-31 Thu .+1d>"
        )

        issue = service.get_issue_for_record(record, extra)
        actual = issue.to_taskwarrior()

        scheduled = datetime.datetime(
            year=2025, month=7, day=1, hour=12, minute=30
        ).astimezone(datetime.UTC)
        deadline = datetime.datetime(year=2025, month=7, day=31).astimezone(
            datetime.UTC
        )
        assert actual["scheduled"] == scheduled
        assert actual["due"] == deadline
        assert actual[issue.SCHEDULED] == scheduled
        assert actual[issue.DEADLINE] == deadline

    def test_issues(self, service, record, extra, page):
        service.client.get_graph_name.return_value = extra["graph"]
        service.client.get_issues.return_value = [[record]]
        service.client.get_page.return_value = page
        issue = next(service.issues())

        expected = {
            "annotations": [],
            "description": f"(bw)#{record['id']}"
            + " - Do something http://example.com/pag"
            + " .. "
            + extra["baseURI"]
            + record["uuid"],
            "due": datetime.datetime(year=2025, month=7, day=31).astimezone(
                datetime.UTC
            ),
            "scheduled": datetime.datetime(year=2025, month=7, day=1).astimezone(
                datetime.UTC
            ),
            "wait": None,
            "status": "pending",
            "priority": "L",
            "project": extra["graph"],
            "tags": [],
            issue.ID: str(record["id"]),
            issue.UUID: record["uuid"],
            issue.STATE: record["marker"],
            issue.TITLE: "Do something http://example.com/page#NotATag `#code`"
            + " #【Test tag one】 #【TestTagTwo】 #TestTagThree",
            issue.URI: extra["baseURI"] + record["uuid"],
            issue.SCHEDULED: datetime.datetime(year=2025, month=7, day=1).astimezone(
                datetime.UTC
            ),
            issue.DEADLINE: datetime.datetime(year=2025, month=7, day=31).astimezone(
                datetime.UTC
            ),
            issue.PAGE: "Jul 1st, 2025",
        }

        assert TaskConstructor(issue).get_taskwarrior_record() == expected
