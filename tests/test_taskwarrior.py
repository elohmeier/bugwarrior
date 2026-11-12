from datetime import UTC, datetime
from unittest import mock

import pytest

from bugwarrior.taskwarrior import TaskWarriorShellout, TaskwShellout


@pytest.mark.parametrize("major", [2, 3])
def test_writer_is_version_specific(major):
    writer = TaskWarriorShellout.__new__(TaskWarriorShellout)
    writer._json_writes = major >= 3
    with (
        mock.patch.object(TaskwShellout, "task_add", return_value="legacy") as legacy,
        mock.patch.object(writer, "_stub_task", return_value={}),
        mock.patch.object(writer, "_json_record", return_value={}),
        mock.patch.object(writer, "_import_task") as json_write,
        mock.patch.object(writer, "get_task", return_value=(1, "json")),
    ):
        assert writer.task_add('"quoted"') == ("json" if major >= 3 else "legacy")
    assert json_write.called == (major >= 3)
    assert legacy.called == (major < 3)


def test_annotation_dates_are_preserved_and_new_entries_are_distinct(
    config_environment,
):
    writer = TaskWarriorShellout(
        config_filename=config_environment.taskrc, marshal=True
    )
    original_entry = "20200102T103000Z"
    record = writer._json_record(
        {
            "description": "Task",
            "annotations": [
                {"description": "Original annotation", "entry": original_entry},
                "New annotation 1",
                "New annotation 2",
            ],
            "entry": datetime(2020, 1, 2, tzinfo=UTC),
        }
    )
    assert record["annotations"][0]["entry"] == original_entry
    assert len({a["entry"] for a in record["annotations"]}) == 3


def test_json_updates_preserve_local_fields_and_clear_dates(config_environment):
    writer = TaskWarriorShellout(
        config_filename=config_environment.taskrc, marshal=True
    )
    if not writer._json_writes:
        pytest.skip("Taskwarrior 3 JSON transport")
    dependency = writer.task_add("Dependency")
    added = writer.task_add(
        '"Original"',
        tags=["local"],
        depends=[dependency["uuid"]],
        annotations=[{"entry": "20200102T103000Z", "description": '"Original note"'}],
        due=datetime(2030, 1, 2, tzinfo=UTC),
        customfield="literal &dquot; [value]",
    )
    added["description"] = '"Updated"'
    added["due"] = None
    writer.task_update(added)
    _, updated = writer.get_task(uuid=added["uuid"])
    assert updated["description"] == '"Updated"'
    assert updated.get("due") is None
    assert updated["depends"] == [dependency["uuid"]]
    assert updated["tags"] == ["local"]
    assert updated["customfield"] == "literal &dquot; [value]"
    assert updated["annotations"][0].entry == datetime(2020, 1, 2, 10, 30, tzinfo=UTC)

    writer.task_update({"uuid": added["uuid"], "description": "Partial update"})
    _, partial = writer.get_task(uuid=added["uuid"])
    assert partial["depends"] == updated["depends"]
    assert partial["customfield"] == updated["customfield"]
    assert partial["annotations"] == updated["annotations"]
