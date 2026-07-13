Tool reference
==============

Every tool the MCP server exposes, grouped by purpose. The descriptions below are generated from
the tool docstrings — the exact contract the assistant itself sees, which is why some read as
instructions addressed to it. Long text in results is truncated (~4k characters per output
payload, ~16k per cell source) unless a tool documents a ``full`` argument; image payloads are
replaced with a stub in text results and fetched through ``read_cell_image``.

.. currentmodule:: nbclassic_mcp_bridge_mcp.server

Attaching
---------

.. autofunction:: use_notebook
.. autofunction:: list_notebooks
.. autofunction:: use_server
.. autofunction:: use_project

Reading
-------

.. autofunction:: read_notebook
.. autofunction:: read_cell_source
.. autofunction:: read_cell_output
.. autofunction:: read_cell_image

Editing
-------

.. autofunction:: insert_cell
.. autofunction:: set_cell_source
.. autofunction:: move_cell
.. autofunction:: delete_cell
.. autofunction:: undo_last_change
.. autofunction:: undo_all_changes

Execution
---------

.. autofunction:: execute_cell
.. autofunction:: run_cells
.. autofunction:: inspect_kernel
.. autofunction:: interrupt_kernel
.. autofunction:: kernel_status

Safety
------

.. autofunction:: checkpoint_notebook
.. autofunction:: restore_notebook_checkpoint

Events
------

.. autofunction:: poll_events
