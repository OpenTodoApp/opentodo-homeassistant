# Changelog

## 0.1.0 (2026-10-25)

First release.

- **To-do lists** for the Inbox, Today and each project: add (with OpenTodo's quick-add syntax), rename, complete, reopen, reschedule, reorder and delete tasks.
- **Counts** for Today, Earlier, the Inbox and each project's open tasks.
- **Calendar** of tasks with a do-on date.
- **Events** `opentodo_task_created`, `_updated`, `_completed`, `_reopened`, `_deleted`, `_assigned` and `_due`.
- **Action** `opentodo.add_task`, which returns the created task.
- **Instant updates** through a signed OpenTodo webhook, with polling and a repair issue when OpenTodo may not call Home Assistant on a private network.
- **Assist** through Home Assistant's list intents.
- Setup with a Full access or Read only token; reauthentication, reconfiguration, options and diagnostics. Translations in English, Italian, German, French, Spanish and Portuguese.
