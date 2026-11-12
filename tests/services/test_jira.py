from collections import namedtuple
from datetime import UTC, datetime
import socket
from unittest import mock
import warnings

from pydantic import ValidationError
import pytest
from urllib3.exceptions import InsecureRequestWarning

from bugwarrior.collect import TaskConstructor
from bugwarrior.config import validation
from bugwarrior.config.load import format_config
from bugwarrior.services.jira import JiraExtraFields, JiraService, _patch_dns_resolve

SERVICE_CLASS = JiraService

SERVICE_CONFIG = {
    "service": "jira",
    "username": "one",
    "base_uri": "https://two.org",
    "password": "three",
    "extra_fields": [
        "jiraextra1:customfield_10000",
        "jiraextra2:namedfield.valueinside",
    ],
}


ESTIMATION = 3600
ARBITRARY_ID = "10"
ARBITRARY_SUBTASK_IDS = ["11", "12"]
ARBITRARY_PARENT_ID = "13"
ARBITRARY_NAMEDFIELD_VALUEINSIDE = 77
PROJECT = "DONUT"
SUMMARY = "lkjaldsfjaldf"


@pytest.fixture
def record():
    return {
        "fields": {
            "priority": "Blocker",
            "summary": SUMMARY,
            "timeestimate": ESTIMATION,
            "created": "2016-06-06T06:07:08.123-0700",
            "fixVersions": [{"name": "1.2.3"}],
            "issuetype": {"name": "Epic"},
            "status": {"name": "Open"},
            "subtasks": [
                {"key": f"DONUT-{subtask}"} for subtask in ARBITRARY_SUBTASK_IDS
            ],
            "parent": {"key": f"DONUT-{ARBITRARY_PARENT_ID}"},
            "customfield_10000": "foo",
            "namedfield": {"valueinside": ARBITRARY_NAMEDFIELD_VALUEINSIDE},
        },
        "key": f"{PROJECT}-{ARBITRARY_ID}",
    }


@pytest.fixture
def record_with_due(record):
    record["fields"]["Sprint"] = [
        "com.atlassian.greenhopper.service.sprint.Sprint@4c9c41a5[id=2322,rapidViewId=1173,\
                    state=ACTIVE,name=Sprint 1,startDate=2016-09-06T16:08:07.4\
                    55Z,endDate=2016-09-23T16:08:00.000Z,completeDate=<null>,sequence=2322]"
    ]
    return record


class FakeJiraClient:
    def __init__(self, record):
        self.record = record

    def search_issues(self, *args, **kwargs):
        Case = namedtuple("Case", ["raw", "key"])
        return [Case(self.record, self.record["key"])]

    def comments(self, *args, **kwargs):
        return None


class TestDnsResolve:
    def test_patch_dns_resolve(self, monkeypatch):
        monkeypatch.setattr(socket, "getaddrinfo", socket.getaddrinfo)
        _patch_dns_resolve("example.invalid", "127.0.0.1")
        results = socket.getaddrinfo("example.invalid", 443)
        addresses = [r[4][0] for r in results]
        assert all(addr == "127.0.0.1" for addr in addresses)

    def test_patch_dns_resolve_passthrough(self, monkeypatch):
        monkeypatch.setattr(socket, "getaddrinfo", socket.getaddrinfo)
        _patch_dns_resolve("example.invalid", "127.0.0.1")
        results = socket.getaddrinfo("localhost", 80)
        addresses = [r[4][0] for r in results]
        assert any(addr in ("127.0.0.1", "::1") for addr in addresses)


class TestJiraService:
    @pytest.fixture
    def config(self):
        return {
            "general": {"targets": ["myservice"]},
            "myservice": {
                "service": "jira",
                "base_uri": "https://example.com",
                "username": "milou",
                "password": "t0ps3cr3t",
                "extra_fields": [
                    "jiraextra1:customfield_10000",
                    "jiraextra2:namedfield.valueinside",
                ],
            },
        }

    @pytest.mark.parametrize(
        ("web_base_uri", "expected_base_uri"),
        [
            (None, "https://api.example.com:9994/jira"),
            ("https://example.com", "https://example.com"),
            ("https://example.com/jira/", "https://example.com/jira"),
        ],
    )
    def test_browser_links_and_api_connection(
        self, config, record, web_base_uri, expected_base_uri
    ):
        config["myservice"]["base_uri"] = "https://api.example.com:9994/jira/"
        if web_base_uri is not None:
            config["myservice"]["web_base_uri"] = web_base_uri
        config["general"]["annotation_links"] = True
        conf = validation.validate_config(
            format_config(config), "general", "configpath"
        )
        client = mock.Mock()
        client.search_issues.return_value = [mock.Mock(raw=record, key=record["key"])]
        client.comments.return_value = []

        with mock.patch("bugwarrior.services.jira.JIRA", return_value=client) as jira:
            service = JiraService(conf.service_configs[0], conf.main)
        jira.assert_called_once_with(
            options={
                "server": "https://api.example.com:9994/jira",
                "rest_api_version": "latest",
                "verify": True,
            },
            basic_auth=("milou", "t0ps3cr3t"),
        )

        issue = next(service.issues())
        task = TaskConstructor(issue).get_taskwarrior_record()
        expected_url = f"{expected_base_uri}/browse/{record['key']}"
        assert task["jiraurl"] == expected_url
        assert task["description"].endswith(f" .. {expected_url}")
        assert task["annotations"] == [expected_url]

    def test_invalid_web_base_uri(self):
        with pytest.raises(ValidationError, match="web_base_uri"):
            JiraService.CONFIG_SCHEMA(**SERVICE_CONFIG, web_base_uri="invalid-url")

    @pytest.mark.parametrize("verify_ssl", [True, False])
    def test_tls_warnings_during_connection_and_collection(self, config, verify_ssl):
        config["myservice"].update(verify_ssl=verify_ssl, import_sprints_as_tags=True)
        conf = validation.validate_config(
            format_config(config), "general", "configpath"
        )
        client = mock.Mock()
        message = "Unverified HTTPS request is being made to host 'example.com'."

        def warn_and_return(result):
            def call(*args, **kwargs):
                warnings.warn(message, InsecureRequestWarning, stacklevel=2)
                return result

            return call

        client.fields.side_effect = warn_and_return(
            [{"name": "Sprint", "id": "sprint"}]
        )
        client.search_issues.side_effect = warn_and_return([])
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            with mock.patch(
                "bugwarrior.services.jira.JIRA", side_effect=warn_and_return(client)
            ):
                service = JiraService(conf.service_configs[0], conf.main)
            # Collection installs its own filter, as it runs in a spawned worker.
            assert list(service.issues()) == []
            assert len(caught) == (3 if verify_ssl else 0)

            # The filter must not survive the Jira request scope.
            warnings.warn(message, InsecureRequestWarning, stacklevel=2)
            assert caught[-1].category is InsecureRequestWarning
            assert len(caught) == (4 if verify_ssl else 1)

    def test_tls_warning_suppression_keeps_other_warnings(self, config):
        config["myservice"]["verify_ssl"] = False
        conf = validation.validate_config(
            format_config(config), "general", "configpath"
        )
        service = JiraService(conf.service_configs[0], conf.main, _skip_server=True)
        service.jira = mock.Mock()
        message = "Unverified HTTPS request is being made to host 'example.com'."
        other_host = "Unverified HTTPS request is being made to host 'exampleXcom'."

        def search(*args, **kwargs):
            warnings.warn(message, InsecureRequestWarning, stacklevel=2)
            warnings.warn(other_host, InsecureRequestWarning, stacklevel=2)
            warnings.warn(message, UserWarning, stacklevel=2)
            return []

        service.jira.search_issues.side_effect = search
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            assert list(service.issues()) == []

        assert [(str(w.message), w.category) for w in caught] == [
            (other_host, InsecureRequestWarning),
            (message, UserWarning),
        ]

    def test_body_length_no_limit(self, config):
        description = "A very short issue body.  Fixes #828."
        config["myservice"]["body_length"] = "5"
        formatted = format_config(config)
        conf = validation.validate_config(formatted, "general", "configpath")
        service = JiraService(conf.service_configs[0], conf.main, _skip_server=True)
        issue = mock.Mock()
        issue.record = {"fields": {"description": description}}
        assert description[:5] == service.body(issue)

    def test_body_length_limit(self, config):
        description = "A very short issue body.  Fixes #828."
        formatted = format_config(config)
        conf = validation.validate_config(formatted, "general", "configpath")
        service = JiraService(conf.service_configs[0], conf.main, _skip_server=True)
        issue = mock.Mock()
        issue.record = {"fields": {"description": description}}
        assert description == service.body(issue)


class TestJiraIssue:
    @pytest.fixture
    def service(self, record, make_service):
        with mock.patch("jira.client.JIRA._get_json"):
            service = make_service()
        service.jira = FakeJiraClient(record)
        service.sprint_field_names = ["Sprint"]
        return service

    def get_extra_fields(self):
        return JiraExtraFields.validate(
            ["jiraextra1:customfield_10000", "jiraextra2:namedfield.valueinside"]
        )

    def test_to_taskwarrior(self, service, record):
        url = "http://one"
        extra = {
            "annotations": ["an annotation"],
            "body": "issue body",
            "sprint_field_names": [],
        }

        issue = service.get_issue_for_record(record, extra)

        expected_output = {
            "project": PROJECT,
            "priority": (issue.PRIORITY_MAP[record["fields"]["priority"]]),
            "annotations": extra["annotations"],
            "due": None,
            "tags": [],
            "entry": datetime(2016, 6, 6, 13, 7, 8, tzinfo=UTC),
            "jirafixversion": "1.2.3",
            "jiraissuetype": "Epic",
            "jirastatus": "Open",
            "jirasubtasks": "DONUT-11,DONUT-12",
            "jiraparent": "DONUT-13",
            "jiraextra1": "foo",
            "jiraextra2": 77,
            issue.URL: url,
            issue.FOREIGN_ID: record["key"],
            issue.SUMMARY: SUMMARY,
            issue.DESCRIPTION: "issue body",
            issue.ESTIMATE: ESTIMATION / 60 / 60,
        }

        def get_url(*args):
            return url

        with mock.patch.object(issue, "get_url", side_effect=get_url):
            actual_output = issue.to_taskwarrior()

        assert actual_output == expected_output

    def test_to_taskwarrior_sprint_with_goal(self, service, record):
        record["fields"]["Sprint"] = [
            "com.atlassian.greenhopper.service.sprint.Sprint@4c9c41a5[id=2322,rapidViewId=1173,\
            state=ACTIVE,name=Sprint 1,goal=Do foo, bar, baz,startDate=2016-09-06T16:08:07.4\
            55Z,endDate=2016-09-23T16:08:00.000Z,completeDate=<null>,sequence=2322]"
        ]
        url = "http://one"
        extra = {
            "annotations": ["an annotation"],
            "sprint_field_names": service.sprint_field_names,
        }

        issue = service.get_issue_for_record(record, extra)

        expected_output = {
            "project": PROJECT,
            "priority": (issue.PRIORITY_MAP[record["fields"]["priority"]]),
            "annotations": extra["annotations"],
            "due": datetime(2016, 9, 23, 16, 8, tzinfo=UTC),
            "tags": [],
            "entry": datetime(2016, 6, 6, 13, 7, 8, tzinfo=UTC),
            "jirafixversion": "1.2.3",
            "jiraissuetype": "Epic",
            "jirastatus": "Open",
            "jirasubtasks": "DONUT-11,DONUT-12",
            "jiraparent": "DONUT-13",
            "jiraextra1": "foo",
            "jiraextra2": 77,
            issue.URL: url,
            issue.FOREIGN_ID: record["key"],
            issue.SUMMARY: SUMMARY,
            issue.DESCRIPTION: None,
            issue.ESTIMATE: ESTIMATION / 60 / 60,
        }

        def get_url(*args):
            return url

        with mock.patch.object(issue, "get_url", side_effect=get_url):
            actual_output = issue.to_taskwarrior()

        assert actual_output == expected_output

    def test_issues(self, service):
        issue = next(service.issues())

        expected = {
            "annotations": [],
            "due": None,
            "description": (
                "(bw)Is#10 - lkjaldsfjaldf .. https://two.org/browse/DONUT-10"
            ),
            "entry": datetime(2016, 6, 6, 13, 7, 8, tzinfo=UTC),
            "jiradescription": None,
            "jiraestimate": 1,
            "jirafixversion": "1.2.3",
            "jiraid": "DONUT-10",
            "jiraissuetype": "Epic",
            "jirastatus": "Open",
            "jirasummary": "lkjaldsfjaldf",
            "jiraurl": "https://two.org/browse/DONUT-10",
            "jirasubtasks": "DONUT-11,DONUT-12",
            "jiraparent": "DONUT-13",
            "jiraextra1": "foo",
            "jiraextra2": 77,
            "priority": "H",
            "project": "DONUT",
            "tags": [],
        }

        assert TaskConstructor(issue).get_taskwarrior_record() == expected

    def test_get_due(self, service, record_with_due):
        issue = service.get_issue_for_record(
            record_with_due, extra={"sprint_field_names": service.sprint_field_names}
        )

        assert issue.get_due() == datetime(2016, 9, 23, 16, 8, tzinfo=UTC)

    def test_get_due_sprint_dict_missing_end_date(self, service, record):
        record["fields"]["Sprint"] = [{"id": 1, "state": "active", "name": "Sprint 1"}]

        issue = service.get_issue_for_record(
            record, extra={"sprint_field_names": service.sprint_field_names}
        )

        assert issue.get_due() is None
