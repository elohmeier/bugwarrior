Apple Reminders
===============

The ``applereminders`` service imports reminders available through EventKit on
macOS 14 or later, including completed history. It reads the local synchronized
store; no iCloud credentials are required. Imports never change Apple Reminders.

Build and authorize the helper
------------------------------

Install Apple's Xcode command line tools if needed (``xcode-select --install``),
then build the bundled Swift reader::

    bugwarrior-reminders-helper
    ~/.local/libexec/bugwarrior-reminders authorize
    ~/.local/libexec/bugwarrior-reminders lists

The build command accepts ``--output /absolute/path/to/helper``. Set
``helper_path`` in the target to use that location. The helper source and privacy
description are included in Bugwarrior's source and wheel distributions.

Apple requires full Reminders permission even for read-only access. Only
``authorize`` requests permission; collection fails if permission is missing,
denied, or revoked. Authorize from the same application or terminal that will
run imports. Scheduled jobs need their own access check; a grant from an
interactive terminal does not guarantee access from launchd. Moving or rebuilding
the executable can require renewed authorization.

Example configuration
---------------------

Add a target to your existing configuration's target list:

.. config::

    [general]
    targets = reminders

    [reminders]
    service = applereminders
    applereminders.namespace = personal-reminders
    add_tags = apple-reminders

The default ``helper_path`` is ``~/.local/libexec/bugwarrior-reminders``.
``timeout`` defaults to 60 seconds. All lists and completed reminders are
included by default. To select lists, use the exact identifiers returned by the
helper's ``lists`` command:

.. config::
    :fragment: applereminders

    applereminders.list_ids = list-identifier-1,list-identifier-2
    applereminders.include_completed = False

An unavailable configured list aborts collection. Use one target for a given
namespace, with all intended lists selected there. Keep the namespace stable:
changing it creates a separate set of Taskwarrior tasks. Independent Reminders
stores should have distinct namespaces. Duplicate identifiers within a snapshot
are rejected rather than merged.

Mapping and archival behavior
-----------------------------

* Full titles become descriptions, without the usual issue description prefix
  or truncation. Notes become full annotations. Original URLs are stored in a
  UDA; the importer does not guess Reminders deep links.
* The list title becomes the project. Use ``project_template`` to override it.
  Original list identifiers and titles are retained in UDAs.
* Creation dates become ``entry``. Explicit completion state and dates become
  ``status`` and ``end``. If a completed reminder has no completion date,
  Taskwarrior supplies an end date; the unknown source date remains in metadata.
* Timed deadlines become ``due``. Date-only deadlines use 23:59:59 on the source
  day. Start dates become ``scheduled``, using midnight for date-only starts.
  Floating dates use the Mac's local timezone; calendar arithmetic handles DST.
  Original date-only and timezone metadata is retained.
* Priority 0 means no priority; 1--4 map to H, 5 to M, and 6--9 to L. The common
  ``static_fields`` setting preserves local priority changes by default.
* Recurrence rules and alarms, including location triggers, are retained as
  source metadata. They do not create Taskwarrior recurrences or notifications.

Only explicit source completion completes imported tasks. Deleting a reminder,
removing a list from the selection, or excluding completed history does not
complete or delete the local copy. Open reminders can reopen completed local
tasks on a later pull. This is a one-way import, so completing a Taskwarrior task
does not complete the Apple reminder.

Identity uses the namespace and external identifier, falling back to a marked
local identifier when EventKit provides no external identifier. List moves and
renames do not change identity. Local identifiers can change after a full store
resynchronization; reminders using that fallback may need manual reconciliation.

Public EventKit APIs do not expose native Reminders tags, sections, attachments,
flags, or subtask relationships. The importer preserves the task records it can
read, with a flat structure; it is not a complete backup of Reminders.app.

Before importing into an existing archive, back up Taskwarrior and check a dry
run for duplicates with tasks already imported through another service. For a
trial import, use a separate Taskwarrior configuration and data directory. No
automatic cross-service deduplication is attempted.

Provided UDA Fields
-------------------

.. udas:: bugwarrior.services.applereminders.AppleRemindersIssue
