import copy
import json
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from bugwarrior.services.logseq_workflow import reconcile_completion, resolve_context

BLOCK = "0b8cb23a-4c5d-48c5-8b27-dd6d9449ba89"


def test_resolves_nearest_project_before_area_and_preserves_context():
    record = {
        "content": "TODO Check the rollout #Work",
        "parent": {"id": 2, "content": "Meeting [[Projects/Rollout]]"},
    }
    pages = {
        "Projects/Rollout": {"properties": {"taskwarrior-project": "Work.Rollout"}}
    }
    project, context = resolve_context(
        record, {}, lambda name: pages.get(name, {}), {"Work": "Work"}
    )
    assert project == "Work.Rollout"
    assert context == "Meeting [[Projects/Rollout]]"


def test_explicit_task_project_overrides_referenced_and_page_projects():
    record = {
        "content": "TODO Check [[Projects/Other]]",
        "properties": {"taskwarrior-project": "Home.Renovation"},
    }
    page = {"properties": {"taskwarrior-project": "Work.Rollout"}}
    project, _ = resolve_context(record, page, Mock(), {})
    assert project == "Home.Renovation"


def test_http_sdk_camel_case_project_properties_are_resolved():
    page = {"properties": {"taskwarriorProject": "Work.Rollout"}}
    project, _ = resolve_context({"content": "TODO Check rollout"}, page, Mock(), {})
    assert project == "Work.Rollout"


def test_area_namespaces_inherit_the_configured_root_area():
    record = {
        "content": "TODO Check rollout",
        "parent": {"id": 1, "content": "[[Work/Meetings/Weekly]]"},
    }
    project, _ = resolve_context(record, {}, lambda _: {}, {"work": "Work"})
    assert project == "Work"


def test_area_resolution_uses_ancestors_not_person_names():
    record = {
        "content": "TODO Ask [[Example Person]]",
        "parent": {"id": 2, "content": "Meeting #Work"},
    }

    def lookup(_):
        return {}

    assert resolve_context(record, {}, lookup, {"work": "Work"})[0] == "Work"
    assert (
        resolve_context(
            {"content": "TODO Ask [[Example Person]]"}, {}, lookup, {"work": "Work"}
        )[0]
        is None
    )


def test_conflicting_project_references_are_left_unclassified():
    pages = {
        "Projects/A": {"properties": {"taskwarrior-project": "Work.A"}},
        "Projects/B": {"properties": {"taskwarrior-project": "Work.B"}},
    }
    project, _ = resolve_context(
        {"content": "TODO Compare [[Projects/A]] and [[Projects/B]]"},
        {},
        lambda name: pages.get(name, {}),
        {},
    )
    assert project is None


@pytest.fixture
def reconciliation(tmp_path):
    record = {
        "uuid": BLOCK,
        "id": 123,
        "marker": "TODO",
        "content": "TODO Source action\nNotes with [[links]] and quotes\nid:: retained",
    }
    task = {
        "uuid": "46f08e38-7350-4a4b-82d2-c9270c0c94fa",
        "logsequuid": BLOCK,
        "logseqid": "123",
        "logsequri": f"logseq://graph/Test?block-id={BLOCK}",
        "logseqstate": "TODO",
        "status": "completed",
        "end": "20250101T120000Z",
        "description": "My curated action",
        "project": "Work.Rollout",
        "wait": "20270101T120000Z",
    }
    client = Mock()
    client.get_graph_name.return_value = "Test"
    client.get_blocks.side_effect = lambda uuids: (
        [copy.deepcopy(record)] if BLOCK in uuids else []
    )

    def update(uuid, content):
        assert uuid == BLOCK
        record.update(content=content, marker=content.split()[0])

    client.update_block.side_effect = update
    service = SimpleNamespace(
        client=client,
        config=SimpleNamespace(graph="Test"),
        main_config=SimpleNamespace(data=SimpleNamespace(path=str(tmp_path))),
    )
    tw = Mock()
    tw.filter_tasks.return_value = [task]
    return service, tw, record, task, tmp_path


@pytest.mark.parametrize(
    ("outcome", "marker"),
    [(None, "DONE"), ("dropped", "CANCELED"), ("merged", "CANCELED")],
)
def test_completion_updates_only_marker_and_keeps_local_planning(
    reconciliation, outcome, marker
):
    service, tw, record, task, tmp_path = reconciliation
    if outcome:
        task["outcome"] = outcome
    original = record["content"]
    reconcile_completion(service, tw, dry_run=False)
    assert record["content"] == marker + original[len("TODO") :]
    assert task["end"] == "20250101T120000Z"
    assert task["project"] == "Work.Rollout"
    assert task["description"] == "My curated action"
    assert task["wait"] == "20270101T120000Z"
    assert task["logseqstate"] == marker
    tw.task_update.assert_called_once_with(task)
    snapshot = next((tmp_path / "logseq-writeback").glob("*.json"))
    assert json.loads(snapshot.read_text())[0]["before"] == original
    assert snapshot.stat().st_mode & 0o777 == 0o600
    service.client.update_block.reset_mock()
    tw.task_update.reset_mock()
    reconcile_completion(service, tw, dry_run=False)
    service.client.update_block.assert_not_called()
    tw.task_update.assert_not_called()


def test_dry_run_changes_neither_store_nor_creates_snapshots(reconciliation):
    service, tw, record, task, tmp_path = reconciliation
    before = copy.deepcopy((record, task))
    reconcile_completion(service, tw, dry_run=True)
    assert (record, task) == before
    service.client.update_block.assert_not_called()
    tw.task_update.assert_not_called()
    assert not (tmp_path / "logseq-writeback").exists()


def test_explicit_source_completion_closes_known_action(reconciliation):
    service, tw, record, task, _ = reconciliation
    task.pop("end")
    task["status"] = "pending"
    record.update(marker="DONE", content="DONE Source action")
    reconcile_completion(service, tw, dry_run=False)
    assert task["status"] == "completed"
    assert task["end"]
    assert task["outcome"] == "done"
    service.client.update_block.assert_not_called()


def test_missing_source_block_does_not_close_action(reconciliation):
    service, tw, _, task, _ = reconciliation
    task["status"] = "pending"
    service.client.get_blocks.side_effect = lambda _: []
    reconcile_completion(service, tw, dry_run=False)
    assert task["status"] == "pending"
    tw.task_update.assert_not_called()


def test_wrong_graph_aborts_before_writes(reconciliation):
    service, tw, _, _, _ = reconciliation
    service.client.get_graph_name.return_value = "Another graph"
    with pytest.raises(ValueError, match="configured graph"):
        reconcile_completion(service, tw, dry_run=False)
    service.client.update_block.assert_not_called()
    tw.task_update.assert_not_called()


def test_concurrent_source_edit_is_preserved(reconciliation):
    service, tw, record, _, _ = reconciliation
    changed = {**record, "content": "TODO Updated by the user"}
    service.client.get_blocks.side_effect = [[copy.deepcopy(record)], [changed]]
    with pytest.raises(ValueError, match="changed during reconciliation"):
        reconcile_completion(service, tw, dry_run=False)
    service.client.update_block.assert_not_called()
    tw.task_update.assert_not_called()


def test_switching_graph_before_source_write_aborts(reconciliation):
    service, tw, _, _, _ = reconciliation
    service.client.get_graph_name.side_effect = ["Test", "Other graph"]
    with pytest.raises(ValueError, match="graph changed"):
        reconcile_completion(service, tw, dry_run=False)
    service.client.update_block.assert_not_called()
    tw.task_update.assert_not_called()
