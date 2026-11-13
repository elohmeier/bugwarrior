from collections.abc import Iterator
from datetime import UTC, datetime
import logging
import re
import sys
import typing
from typing import Any
from uuid import UUID

from dateutil.tz import tzlocal
from pydantic import model_validator
import requests

from bugwarrior import config
from bugwarrior.services import Client, Issue, Service
from bugwarrior.services.logseq_workflow import reconcile_completion, resolve_context

log = logging.getLogger(__name__)


class LogseqConfig(config.ServiceConfig):
    service: typing.Literal["logseq"]
    KEYRING_SERVICE = "http://{host}:{port}"
    host: str = "localhost"
    port: int = 12315
    token: str
    task_state: config.ConfigList = [
        "DOING",
        "TODO",
        "NOW",
        "LATER",
        "IN-PROGRESS",
        "WAIT",
        "WAITING",
        # states DONE and CANCELED/CANCELLED are skipped by default
    ]
    char_open_link: str = "【"
    char_close_link: str = "】"
    char_open_bracket: str = "〈"
    char_close_bracket: str = "〉"
    inline_links: bool = True
    import_labels_as_tags: bool = False
    label_template: str = "{{label}}"
    graph: str | None = None
    area_projects: dict[str, str] = {}
    sync_completion: bool = False

    @model_validator(mode="after")
    def validate_local_workflow(self) -> "LogseqConfig":
        if self.sync_completion and (
            self.reopen_completed or self.complete_missing is not False
        ):
            raise ValueError(
                "sync_completion requires reopen_completed=false and complete_missing=false"
            )
        return self

    only_if_assigned: config.UnsupportedOption[str] = ""
    also_unassigned: config.UnsupportedOption[bool] = False


class LogseqClient(Client):
    def __init__(self, host: str, port: int, token: str, filter: str) -> None:
        self.host = host
        self.port = port
        self.token = token
        self.filter = filter

        self.headers = {
            "Authorization": "Bearer " + self.token,
            "content-type": "application/json; charset=utf-8",
        }

    def _datascript_query(self, query: str) -> Any:
        try:
            response = requests.post(
                f"http://{self.host}:{self.port}/api",
                headers=self.headers,
                json={"method": "logseq.DB.datascriptQuery", "args": [query]},
            )
            return self.json_response(response)
        except requests.exceptions.ConnectionError as ce:
            log.fatal("Unable to connect to Logseq HTTP APIs server. %s", ce)
            sys.exit(1)

    def _get_current_graph(self) -> dict[str, Any]:
        try:
            response = requests.post(
                f"http://{self.host}:{self.port}/api",
                headers=self.headers,
                json={"method": "logseq.getCurrentGraph", "args": []},
            )
            return self.json_response(response)
        except requests.exceptions.ConnectionError as ce:
            log.fatal("Unable to connect to Logseq HTTP APIs server. %s", ce)
            sys.exit(1)

    def get_graph_name(self) -> str | None:
        graph = self._get_current_graph()
        return graph["name"] if graph else None

    def get_page(self, page_id: int) -> dict[str, Any]:
        try:
            response = requests.post(
                f"http://{self.host}:{self.port}/api",
                headers=self.headers,
                json={"method": "logseq.getPage", "args": [page_id]},
            )
            return self.json_response(response)
        except requests.exceptions.ConnectionError as ce:
            log.fatal("Unable to connect to Logseq HTTP APIs server. %s", ce)
            sys.exit(1)

    def get_issues(self) -> Any:
        parent = "[:db/id :block/content :block/properties]"
        for _ in range(7):
            parent = (
                "[:db/id :block/content :block/properties "
                "{:block/parent " + parent + "}]"
            )
        query = f"""
            [:find (pull ?b [* {{:block/parent {parent}}}])
                :where [?b :block/marker ?marker]
                [(contains? #{{{self.filter}}} ?marker)]
            ]
        """
        result = self._datascript_query(query)
        if "error" in result:
            log.fatal(
                "Error querying Logseq: %s using query %s", result["error"], query
            )
            sys.exit(1)
        return result

    def get_blocks(self, uuids: list[str]) -> list[dict[str, Any]]:
        if not uuids:
            return []
        identifiers = " ".join(f'#uuid "{UUID(value)}"' for value in uuids)
        query = (
            "[:find (pull ?b [:db/id :block/uuid :block/content :block/marker]) "
            ":where [?b :block/uuid ?u] "
            f"[(contains? #{{{identifiers}}} ?u)]]"
        )
        return [row[0] for row in self._datascript_query(query)]

    def update_block(self, uuid: str, content: str) -> None:
        response = requests.post(
            f"http://{self.host}:{self.port}/api",
            headers=self.headers,
            json={"method": "logseq.Editor.updateBlock", "args": [uuid, content]},
            timeout=30,
        )
        result = self.json_response(response)
        if isinstance(result, dict) and result.get("error"):
            raise RuntimeError("Logseq rejected a completion update")


class LogseqIssue(Issue):
    ID = "logseqid"
    UUID = "logsequuid"
    STATE = "logseqstate"
    TITLE = "logseqtitle"
    DONE = "logseqdone"
    URI = "logsequri"
    SCHEDULED = "logseqscheduled"
    DEADLINE = "logseqdeadline"
    PAGE = "logseqpage"
    CONTEXT = "logseqcontext"
    PROJECT = "logseqproject"

    # Local 2038-01-18, with time 00:00:00.
    # A date far away, with semantically meaningful to GTD users.
    # see https://taskwarrior.org/docs/dates/
    SOMEDAY = datetime(2038, 1, 18)

    UDAS = {
        ID: {"type": "string", "label": "Logseq ID"},
        UUID: {"type": "string", "label": "Logseq UUID"},
        STATE: {"type": "string", "label": "Logseq State"},
        TITLE: {"type": "string", "label": "Logseq Title"},
        DONE: {"type": "date", "label": "Logseq Done"},
        URI: {"type": "string", "label": "Logseq URI"},
        SCHEDULED: {"type": "date", "label": "Logseq Scheduled"},
        DEADLINE: {"type": "date", "label": "Logseq Deadline"},
        PAGE: {"type": "string", "label": "Logseq Page"},
        CONTEXT: {"type": "string", "label": "Logseq Context"},
        PROJECT: {"type": "string", "label": "Source Project"},
        "waitingfor": {"type": "string", "label": "Waiting For"},
        "outcome": {"type": "string", "label": "Outcome"},
        "mergedinto": {"type": "string", "label": "Merged Into"},
    }

    UNIQUE_KEY = (ID, UUID)

    # map A B C priority to H M L
    PRIORITY_MAP: dict[str, config.Priority] = {"A": "H", "B": "M", "C": "L"}

    # `pending` is the defuault state. Taskwarrior will dynamcily change task to `waiting`
    # state if wait date is set to a future date.
    STATE_MAP: dict[str, str] = {
        "IN-PROGRESS": "pending",
        "DOING": "pending",
        "TODO": "pending",
        "NOW": "pending",
        "LATER": "pending",
        "WAIT": "pending",
        "WAITING": "pending",
        "DONE": "completed",
        "CANCELED": "deleted",
        "CANCELLED": "deleted",
    }

    # replace characters that cause escaping issues like [] and "
    # this is a workaround for https://github.com/ralphbean/taskw/issues/172
    def _unescape_content(self, content: str) -> str:
        return (
            content.replace('"', "'")  # prevent &dquote; in task details
            .replace(
                "[[", self.config.char_open_link
            )  # alternate brackets for linked items
            .replace("]]", self.config.char_close_link)
            .replace("[", self.config.char_open_bracket)  # prevent &open; and &close;
            .replace("]", self.config.char_close_bracket)
        )

    # remove brackets and spaces to compress display format of mutli work tags
    # e.g from #[[Multi Word]] to #MultiWord
    def _compress_tag_format(self, tag: str) -> str:
        return (
            tag.replace(self.config.char_open_link, "")
            .replace(" ", "")
            .replace(self.config.char_close_link, "")
        )

    # get an optimized and formatted title
    def get_formatted_title(self) -> str:
        # use first line only and remove state and priority
        first_line = (
            self.record["content"]
            .split("\n")[0]  # only use first line
            .split(self.get_logseq_state() + " ")[1]  # remove state marker
            .replace("[#A] ", "")  # remove priority markers
            .replace("[#B] ", "")
            .replace("[#C] ", "")
        )
        return self._unescape_content(first_line)

    # get a list of tags from the task content
    def get_tags_from_content(self) -> list[str]:
        # pattern match for #[[multi word]] tags and #single word tags
        # but ignore any non-tag use of the # character in URLs
        # like http://example.com/page#test or in `#code`
        # Regex Pattern: (?<=\s)(#【.*?】|#\S+)
        # Note that this is processed after the content is unescaped,
        # so we use the char_open_link and char_close_link
        tags = re.findall(
            r"(?<=\s)"
            + "(#"
            + self.config.char_open_link
            + r".*?"
            + self.config.char_close_link
            + r"|#\S+"
            + ")",
            self.get_formatted_title(),
        )
        # compress format to single words and strip leading `#`
        tags = [self._compress_tag_format(t).lstrip("#") for t in tags]
        return tags

    # get a list of annotations from the content
    def get_annotations_from_content(
        self,
    ) -> tuple[list[str], datetime | None, datetime | None]:
        annotations = []
        scheduled_date = None
        deadline_date = None
        in_logbook = False
        for line in self.record["content"].split("\n"):
            # Ignore things which are only useful within Logseq
            if in_logbook:
                if line.startswith(":END:"):
                    in_logbook = False

                continue

            if line.startswith(":LOGBOOK:"):
                in_logbook = True
                continue

            if line.startswith("id::"):
                continue

            # handle special annotations
            if line.startswith("SCHEDULED: "):
                scheduled_date = self.get_scheduled_date(line)
            elif line.startswith("DEADLINE: "):
                deadline_date = self.get_scheduled_date(line)
            else:
                annotations.append(self._unescape_content(line))
        annotations.pop(0)  # remove first line
        return annotations, scheduled_date, deadline_date

    def get_url(self) -> str:
        return f"logseq://graph/{self.extra['graph']}?block-id={self.record['uuid']}"

    def get_logseq_state(self) -> str:
        return self.record["marker"]

    def get_scheduled_date(self, scheduled: str) -> datetime | None:
        # format is <YYYY-MO-DD DAY HH:MM .+1d>
        # e.g. <2024-06-20 Thu 10:55 .+1d>
        date_split = (
            scheduled.replace("DEADLINE: <", "")
            .replace("SCHEDULED: <", "")
            .replace(">", "")
            .strip()
            .split(" ")
        )
        if (
            len(date_split) == 2
            or len(date_split) == 3
            and (date_split[2][0] in ("+", "."))
        ):  # <date day>
            date = date_split[0]
            date_format = "%Y-%m-%d"
        elif len(date_split) == 3 or len(date_split) == 4:  # <date day time>
            date = date_split[0] + " " + date_split[2]
            date_format = "%Y-%m-%d %H:%M"
        else:
            log.warning(f"Could not determine date format from {scheduled}")
            return None

        try:
            # Logseq dates are local wall time; Taskwarrior exports them in UTC.
            return (
                datetime.strptime(date, date_format)
                .replace(tzinfo=tzlocal())
                .astimezone(UTC)
            )
        except ValueError:
            log.warning(f"Could not parse date {date} from {scheduled}")
        return None

    def _is_waiting(self) -> bool:
        return self.get_logseq_state() in ["WAIT", "WAITING"]

    def to_taskwarrior(self) -> dict[str, Any]:
        annotations, scheduled_date, deadline_date = self.get_annotations_from_content()
        someday = self.SOMEDAY.replace(tzinfo=tzlocal()).astimezone(UTC)
        wait_date = min(
            d for d in [scheduled_date, deadline_date, someday] if d is not None
        )
        result = {
            "project": self.extra["graph"],
            "priority": self.get_priority(),
            "annotations": annotations,
            "tags": self.get_tags_from_labels(self.get_tags_from_content()),
            "due": deadline_date,
            "scheduled": scheduled_date,
            "wait": wait_date if self._is_waiting() else None,
            "status": self.STATE_MAP[self.get_logseq_state()],
            self.ID: str(int(self.record["id"])),
            self.UUID: self.record["uuid"],
            self.STATE: self.record["marker"],
            self.TITLE: self.get_formatted_title(),
            self.URI: self.get_url(),
            self.SCHEDULED: scheduled_date,
            self.DEADLINE: deadline_date,
            self.PAGE: self.extra["page_title"],
        }
        for field in (self.CONTEXT, self.PROJECT):
            if self.extra.get(field):
                result[field] = self.extra[field]
        return result

    def get_default_description(self) -> str:
        return self.build_default_description(
            title=self.get_formatted_title(),
            url=self.get_url() if self.config.inline_links else "",
            number=self.record["id"],
            cls="task",
        )


class LogseqService(Service[LogseqIssue]):
    API_VERSION = 2.0
    ISSUE_CLASS = LogseqIssue
    CONFIG_SCHEMA = LogseqConfig

    def __init__(
        self, config: LogseqConfig, main_config: config.MainSectionConfig
    ) -> None:
        super().__init__(config, main_config)
        self.token = self.get_secret("token")
        filter = '"' + '" "'.join(self.config.task_state) + '"'
        self.client = LogseqClient(
            host=self.config.host,
            port=self.config.port,
            token=self.token,
            filter=filter,
        )

    def issues(self) -> Iterator[LogseqIssue]:
        graph_name = self.client.get_graph_name()
        if self.config.graph and graph_name != self.config.graph:
            raise ValueError("The open Logseq graph differs from the configured graph")
        pages = {}

        def get_page(identity: Any) -> dict[str, Any]:
            if identity not in pages:
                pages[identity] = self.client.get_page(identity) or {}
            return pages[identity]

        for issue in self.client.get_issues():
            record = issue[0]
            page = get_page(record["page"]["id"])
            project, context = resolve_context(
                record, page, get_page, self.config.area_projects
            )
            extra = {
                "graph": graph_name,
                "page_title": page.get("originalName"),
                LogseqIssue.PROJECT: project,
                LogseqIssue.CONTEXT: context,
            }
            yield self.get_issue_for_record(record, extra)

    def reconcile_local_tasks(self, tw: Any, *, dry_run: bool) -> None:
        reconcile_completion(self, tw, dry_run=dry_run)
