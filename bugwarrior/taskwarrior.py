"""Lossless JSON writes for Taskwarrior 3; taskw compatibility for version 2."""

from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
import json
import os
import subprocess
from typing import Any
from uuid import uuid4

from taskw import TaskWarriorShellout as TaskwShellout
from taskw.exceptions import TaskwarriorError
from taskw.fields import StringField
from taskw.task import Task


class LiteralStringField(StringField):
    def serialize(self, value: Any) -> Any:
        return str(value) if value is not None else None

    def deserialize(self, value: Any) -> Any:
        return self.serialize(value)


def literal_fields(fields: Mapping[str, Any]) -> dict[str, Any]:
    return {
        key: LiteralStringField(label=field.label, read_only=field.read_only)
        if type(field) is StringField
        else field
        for key, field in fields.items()
    }


class LiteralTask(Task):
    FIELDS = literal_fields(Task.FIELDS)


class TaskWarriorShellout(TaskwShellout):
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._json_writes = int(str(self.get_version()).split(".")[0]) >= 3

    def _get_task_object(self, obj: dict[str, Any]) -> Any:
        if self._marshal and self._json_writes:
            return LiteralTask(obj, udas=literal_fields(self.config.get_udas()))
        return super()._get_task_object(obj)

    def task_add(
        self, description: str, tags: list[str] | None = None, **kwargs: Any
    ) -> Any:
        if not self._json_writes:
            return super().task_add(description, tags=tags, **kwargs)
        record = self._json_record({"description": description.strip(), **kwargs})
        if tags is not None:
            record["tags"] = tags
        record["uuid"] = str(uuid4())
        self._import_task(record)
        return self.get_task(uuid=record["uuid"])[1]

    def task_update(self, task: Mapping[str, Any]) -> Any:
        if not self._json_writes:
            return super().task_update(task)
        if "uuid" not in task:
            raise KeyError("Task must have a UUID.")
        if not isinstance(task, Task):
            # JSON import replaces the full record. Preserve unspecified local
            # fields when callers supply only a partial update dictionary.
            _, original = self.get_task(uuid=task["uuid"])
            task = {**original, **task}
        record = self._json_record(task)
        record.pop("modified", None)
        self._import_task(record)
        return self.get_task(uuid=record["uuid"])

    def _json_record(self, task: Mapping[str, Any]) -> dict[str, Any]:
        marshalled = (
            task
            if isinstance(task, Task)
            else LiteralTask.from_stub(
                dict(task), udas=literal_fields(self.config.get_udas())
            )
        )
        record = {
            key: value
            for key, value in marshalled.serialized().items()
            if value is not None and key not in ("id", "urgency")
        }
        # taskw's dependency serializer retains the TW2 comma-separated form.
        # TW3 JSON uses an array, including when dependencies are unchanged.
        if "depends" in record:
            record["depends"] = [str(value) for value in marshalled.get("depends", [])]
        # taskw's annotation serializer discards entry dates. Preserve them
        # explicitly and assign distinct timestamps to newly imported notes.
        annotations = []
        used_entries = set()
        next_entry = datetime.now(UTC).replace(microsecond=0)
        for annotation in task.get("annotations", []):
            if isinstance(annotation, dict):
                description = annotation["description"]
                entry = annotation.get("entry")
            else:
                description = str(annotation)
                entry = getattr(annotation, "entry", None)
            if isinstance(entry, datetime):
                entry = entry.astimezone(UTC).strftime("%Y%m%dT%H%M%SZ")
            if not entry:
                while next_entry.strftime("%Y%m%dT%H%M%SZ") in used_entries:
                    next_entry += timedelta(seconds=1)
                entry = next_entry.strftime("%Y%m%dT%H%M%SZ")
            used_entries.add(entry)
            annotations.append({"entry": entry, "description": description})
        if annotations or "annotations" in record:
            record["annotations"] = annotations
        return record

    def _import_task(self, record: dict[str, Any]) -> None:
        # taskw's command-line encoder substitutes HTML entities for quotes and
        # brackets. TW3 stores these literally; even raw CLI attributes strip
        # some surrounding quotes. JSON avoids both transformations.
        command = ["task", *self.get_configuration_override_args(), "import", "-"]
        result = subprocess.run(
            command,
            input=json.dumps([record]),
            env={**os.environ, "TASKRC": str(self.config_filename)},
            capture_output=True,
            text=True,
        )
        if result.returncode:
            raise TaskwarriorError(
                command, result.stderr, result.stdout, result.returncode
            )
