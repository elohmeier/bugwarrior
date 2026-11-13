"""Project context and opt-in completion reconciliation for Logseq captures."""

from collections.abc import Callable
from datetime import UTC, datetime
import json
import logging
import os
from pathlib import Path
import re
from typing import Any
from uuid import uuid4

log = logging.getLogger(__name__)

TERMINAL = {"DONE", "CANCELED", "CANCELLED"}
OPEN = {"TODO", "DOING", "NOW", "LATER", "IN-PROGRESS", "WAIT", "WAITING"}


def property_text(value: Any) -> str:
    if isinstance(value, (list, tuple, set)):
        return ", ".join(str(item) for item in value)
    return str(value) if value is not None else ""


def references(content: str) -> list[str]:
    return list(
        dict.fromkeys(
            [*re.findall(r"\[\[([^\]]+)\]\]", content)]
            + re.findall(r"(?<!\S)#([\w/-]+)", content)
        )
    )


def resolve_context(
    record: dict[str, Any],
    page: dict[str, Any],
    get_page: Callable[[Any], dict[str, Any]],
    areas: dict[str, str],
) -> tuple[str | None, str]:
    """Resolve explicit projects before area hints; never guess from task words."""
    nodes = [record]
    node = record.get("parent", {})
    seen = set()
    while node and node.get("id") not in seen and len(nodes) < 10:
        seen.add(node.get("id"))
        nodes.append(node)
        node = node.get("parent", {})
    context = " / ".join(
        dict.fromkeys(
            str(n["content"]).split("\n")[0][:240]
            for n in reversed(nodes[1:])
            if n.get("content")
        )
    )[:2000]
    nodes.append(page)

    def project_of(node: dict[str, Any]) -> str | None:
        props = node.get("properties", {})
        # The HTTP SDK camel-cases property keys, whereas raw fixtures and
        # exported Markdown retain their hyphenated spelling.
        value = property_text(
            props.get("taskwarrior-project", props.get("taskwarriorProject"))
        )
        return value if re.fullmatch(r"[\w-]+(?:\.[\w-]+)*", value) else None

    linked_pages = []
    for node in nodes:
        if project := project_of(node):
            return project, context
        props = node.get("properties", {})
        names = references(str(node.get("content", "")))
        names += references(property_text(props.get("project")))
        linked = [get_page(name) for name in dict.fromkeys(names)]
        linked_pages.append(linked)
        projects = {project_of(p) for p in linked} - {None}
        if len(projects) == 1:
            return projects.pop(), context
        if len(projects) > 1:
            return None, context

    aliases = {name.casefold(): project for name, project in areas.items()}
    for node, linked in zip(nodes, linked_pages, strict=True):
        value = property_text(node.get("properties", {}).get("area"))
        names = references(value) or [value]
        names += references(str(node.get("content", "")))
        names += [str(node.get("originalName", node.get("name", "")))]
        for p in linked:
            value = property_text(p.get("properties", {}).get("area"))
            names += references(value) or [value]
        # A namespace such as Work/Meetings carries its root area even when
        # that referenced page has no properties of its own.
        candidates = [n.casefold() for n in names]
        candidates += [n.split("/", 1)[0] for n in candidates if "/" in n]
        projects = {aliases[n] for n in candidates if n in aliases}
        if len(projects) == 1:
            return projects.pop(), context
        if len(projects) > 1:
            return None, context
    return None, context


def snapshot_changes(path: Path, changes: list[dict[str, Any]]) -> None:
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    filename = path / f"{datetime.now(UTC):%Y%m%dT%H%M%SZ}-{uuid4()}.json"
    with os.fdopen(
        os.open(filename, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "w"
    ) as f:
        json.dump(changes, f, ensure_ascii=False, indent=2)


def reconcile_completion(service: Any, tw: Any, *, dry_run: bool) -> None:
    """Mirror terminal decisions only, retaining source content and local planning."""
    graph = service.client.get_graph_name()
    if service.config.graph and graph != service.config.graph:
        raise ValueError("The open Logseq graph differs from the configured graph")
    prefix = f"logseq://graph/{graph}?block-id="
    tasks = [
        task
        for task in tw.filter_tasks(
            {
                "and": [("logsequuid.any", None)],
                "or": [
                    ("status", "pending"),
                    ("status", "waiting"),
                    ("status", "completed"),
                ],
            }
        )
        if str(task.get("logsequri", "")).startswith(prefix)
    ]
    by_block = {}
    for task in tasks:
        uuid = str(task["logsequuid"])
        if uuid in by_block:
            raise ValueError("Multiple local tasks refer to one Logseq block")
        by_block[uuid] = task
    records = service.client.get_blocks(list(by_block))
    outgoing = []
    incoming = []
    for record in records:
        uuid = str(record["uuid"])
        task = by_block[uuid]
        if str(int(record["id"])) != str(task.get("logseqid")):
            raise ValueError(
                "Logseq block identity changed; reconcile before writing back"
            )
        marker = record.get("marker")
        if task["status"] == "completed" and marker in OPEN:
            target = (
                "CANCELED" if task.get("outcome") in {"dropped", "merged"} else "DONE"
            )
            content = record["content"]
            replacement, count = re.subn(
                rf"\A{re.escape(marker)}(?=\s)", target, content, count=1
            )
            if count != 1:
                raise ValueError("Logseq task marker does not match its source content")
            outgoing.append(
                {
                    "task_uuid": str(task["uuid"]),
                    "block_uuid": uuid,
                    "before": content,
                    "after": replacement,
                    "marker": target,
                }
            )
        elif marker in TERMINAL and (
            task["status"] != "completed" or task.get("logseqstate") != marker
        ):
            incoming.append((task, marker))

    log.info(
        "%sLogseq completion: %d source updates, %d local updates",
        "Would reconcile " if dry_run else "Reconciling ",
        len(outgoing),
        len(incoming),
    )
    if dry_run:
        return
    if outgoing:
        snapshot_changes(
            Path(service.main_config.data.path) / "logseq-writeback", outgoing
        )
    for change in outgoing:
        current = service.client.get_blocks([change["block_uuid"]])
        if len(current) != 1 or current[0]["content"] != change["before"]:
            raise ValueError(
                "Logseq block changed during reconciliation; source left untouched"
            )
        if service.client.get_graph_name() != graph:
            raise ValueError("The open Logseq graph changed during reconciliation")
        service.client.update_block(change["block_uuid"], change["after"])
        actual = service.client.get_blocks([change["block_uuid"]])
        if len(actual) != 1 or actual[0]["content"] != change["after"]:
            raise RuntimeError("Logseq completion update could not be verified")
        incoming.append((by_block[change["block_uuid"]], change["marker"]))
    for task, marker in incoming:
        task["logseqstate"] = marker
        if task["status"] != "completed":
            task["status"] = "completed"
            task["end"] = datetime.now(UTC)
        if not task.get("outcome"):
            task["outcome"] = "done" if marker == "DONE" else "dropped"
        tw.task_update(task)
