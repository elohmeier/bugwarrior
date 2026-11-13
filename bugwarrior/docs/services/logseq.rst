Logseq
======

You can import `tasks <https://docs.logseq.com/#/page/tasks>`_ from `Logseq <https://logseq.com/>`_ using the ``logseq`` service name.


Additional Requirements
-----------------------

To use bugwarrior to pull tickets from Logseq you need to enable the Logseq HTTP APIs server.
In Logseq go to **Settings** > **Features** and toggle the **HTTP APIs server** option.

Next select the **API** option in the top menu to configure authorization token, e.g

.. image:: pictures/logseq_token.png


Example Service
---------------

Here's an example of a Logseq target:

.. config::

    [my_logseq_graph]
    service = logseq
    logseq.token = mybugwarrioraccesstoken

The above example is the minimum required to import issues from Logseq.
You can also feel free to use any of the configuration options described in
:ref:`common_configuration_options` or described in `Service Features`_ below.

Service Features
----------------

Host and port
+++++++++++++

By default the service connects to Logseq on your local machine at `localhost:12315`. If you have
Logseq on another host or using a different port you can change the setting using:

.. config::
    :fragment: logseq

    logseq.host = anotherhost.home.lan
    logseq.port = 12315


Authorization Token
+++++++++++++++++++

The authorization token is used to authenticate with Logseq. This value is required and must match
the one of the authorization tokens set in Logseq HTTP APIs server settings.

.. config::
    :fragment: logseq

    logseq.token = mybugwarrioraccesstoken


Locally owned actions and project context
++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++

For a Taskwarrior-owned action workflow, the following TOML options preserve
local planning and enable narrowly scoped completion reconciliation::

    [notes]
    service = "logseq"
    graph = "My graph"
    # Keep the existing token configuration.
    sync_completion = true
    reopen_completed = false
    complete_missing = false
    static_fields = ["project", "description", "due", "scheduled", "wait"]
    project_template = "{{logseqproject or 'Inbox'}}"
    description_template = "{{logseqtitle}}"
    area_projects = { work = "Work", home = "Home" }

The configured graph name is checked before collection and reconciliation.
Containing pages are resolved from the block's page identity, including nested
tasks. ``logseqcontext`` retains ancestor headings; ``logseqproject`` retains the
source project hint independently of a curated local project.

Set a ``taskwarrior-project`` page/block property for an explicit Taskwarrior
project. Resolution prefers explicit task properties, then the nearest ancestor
or referenced project page, then the containing page. ``area`` properties and
recognized area references or namespaces can supply a root using ``area_projects``. Conflicting
references remain unclassified. Existing task projects are protected by the
example's ``static_fields``; applying new classification to existing tasks is an
explicit migration.

With ``sync_completion`` enabled, a successful pull mirrors local completion to
the linked Logseq marker. The default is ``DONE``; ``outcome:dropped`` and
``outcome:merged`` use ``CANCELED``. The rest of the block is preserved. Known
blocks explicitly completed/cancelled in Logseq also close locally. Missing
blocks are left alone. Source UUID, numeric identity, graph and content are
checked; concurrent source edits abort the affected write. Completion is
eventually synchronized on a pull, not by a Taskwarrior hook. Reopening a locally
completed action remains an explicit local decision.

Before source writes, private preimages are saved below
``<task data>/logseq-writeback/``. Dry runs read the source and report counts but
write neither source markers, snapshots nor local task changes. This mode
requires ``reopen_completed = false`` and ``complete_missing = false``.

The optional local workflow fields ``waitingfor``, ``outcome`` and ``mergedinto``
are declared alongside the source UDAs. They are not imported from note text.

Task filters
++++++++++++

You can configure the service to import tasks in different states.
By default the service will import all tasks in an active tasks states

    DOING, TODO, NOW, LATER, IN-PROGRESS, WAIT, WAITING

You can override this filter by setting the ``task_state`` option to a 
comma separated list of required task states.

.. config::
    :fragment: logseq

    logseq.task_state = DOING, NOW, IN-PROGRESS

Task state and data/time mappings
+++++++++++++++++++++++++++++++++

``DOING``, ``TODO``, ``NOW``, ``LATER``, and ``IN-PROGRESS`` are mapped to the default ``pending`` state.
The Logseq task ``SCHEDULED:`` and ``DEADLINE:`` fields are mapped to the ``scheduled`` and 
``due`` date fields.

``WAITING`` and ``WAIT`` are dynamically mapped to either ``pending`` or ``waiting`` states based on 
the ``wait`` date. The ``SCHEDULED:`` date or ``DEADLINE`` date is used to set the ``wait`` date on the
task. If no scheduled or deadline date is available then the wait date is set to ``someday`` 
(see ``Date and Time Synonyms <https://taskwarrior.org/docs/dates/#synonyms-hahahugoshortcode30s0hbhb/>``_).
Future dated waiting tasks can be listed using ``task waiting``

``DONE`` is mapped to the ``completed`` state.

``CANCELED`` and ``CANCELLED`` are mapped to the ``deleted`` state.

Priority mapping
++++++++++++++++

Logseq task priorities ``A``, ``B``, and ``C`` are mapped to the taskwarrior priorities
``H``, ``M``, and ``L`` respectively.

Character replacement
+++++++++++++++++++++

This capability is in part to workaround ``ralphbean/taskw#172 <https://github.com/ralphbean/taskw/issues/172>``_
which causes the ``[`` and ``]`` characters commonly used in Logseq to be over escaped as ``&open;`` and ``&close;``
when they are synced using bugwarrior.

To avoid display issues ``[[`` and ``]]`` are replaced by ``【`` and ``】`` for page links, and single
``[`` and ``]`` are replaced by ``〈`` and ``〉``. 

You can override this default behaviour to use alternative custom characters by setting the ``char_*`` options.

.. config::
    :fragment: logseq

    logseq.char_open_link = 〖
    logseq.char_close_link = 〗
    logseq.char_open_bracket = (
    logseq.char_close_bracket = )

Import Labels as Tags
+++++++++++++++++++++

Logseq tasks with ``#tag`` style page references in the description can be added to Taskwarrior as tags
using the use the ``import_labels_as_tags`` option.

.. config::
    :fragment: logseq

    logseq.import_labels_as_tags = True

Single- and multi-word tags using the Logseq ``#[[Tag]]`` or ``#[[Multi Word]]`` format are always
condensed to ``Tag`` and ``MultiWord`` before adding as Taskwarrior tags. The format of
the page reference in the task description is unchanged.

To futher control how labels are formatted, you can specify a template used for converting the Logseq tagged page
reference into a Taskwarrior tag. For example, to force all tags to be in lower case, and retain the `#` prefiex 
you could add the following configuration option:

.. config::
    :fragment: logseq

    logseq.label_template = #{{label|lower}}

If you change the tag format you may also need to set the general option ``replace_tags = True`` to replace existing
tags that used the previous format.

See :ref:`field_templates` for more more details on building templates.

Logseq URI links
++++++++++++++++

A ``logseq://`` URI is generated for each task to enable easy navigation directly to the specific task in
the Logseq application. 

By default bugwarrior incorporates the links into task description. To disable this behaviour either 
modify the ``inline_links`` option in the main section to affect all services, or to modify for the logseg sevice only you can 
set it in your Logseq section.

.. config::
    :fragment: logseq
    
    logseq.inline_links = False

Unlike regular ``http://`` links, most terminals do not make application specific URIs clickable. 
A simple way to quickly open a a task in Logseq from the command line is to add a helper function to your 
shell that extacts the Logseq URI and opens it using the system specific launcher. For example, to open the
Logseq URI in MacOS add the following to your ``~/..zshrc``

.. code-block:: bash

    # open a specific taskwarrior task in Logseq
    function taskopen() {
        open $(task $1 | grep "Logseq URI" | sed -r 's/^Logseq URI//')
    }

From the command line you can open a specific task using taskwarior task id, e.g. ``taskopen 1234``.

Troubleshooting
---------------

Logseq graph re-index
+++++++++++++++++++++

Re-indexing can change numeric block identities and UUIDs that were not persisted
in the notes. Check the dry run before importing after a re-index. Existing
identities must be reconciled explicitly to avoid duplicate actions. With
``complete_missing = false``, old tasks remain open rather than being silently
closed. Completion write-back refuses a mismatched numeric identity.

Logseq API connection issues
++++++++++++++++++++++++++++

If you get the following error when running bugwarrior:

    CRITICAL:bugwarrior.services.logseq:Unable to connect to Logseq HTTP APIs server. HTTPConnectionPool(host='localhost', port=12315): Max retries exceeded with url: /api (Caused by NewConnectionError('<urllib3.connection.HTTPConnection object at 0x105764f20>: Failed to establish a new connection: [Errno 61] Connection refused'))

- Check that the LogSeq application is running
- Check that the HTTP APIs server is started
- Check that authorization token is set in the API server settings and matches the 
  ``token``.

Provided UDA Fields
-------------------

.. udas:: bugwarrior.services.logseq.LogseqIssue
