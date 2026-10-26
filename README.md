# OpenTodo for Home Assistant

A Home Assistant integration for [OpenTodo](https://github.com/OpenTodoApp/OpenTodo), the self-hosted task manager. It shows your OpenTodo lists as Home Assistant to-do lists, counts what is due, puts dated tasks on a calendar, and fires events when tasks change, so automations can react.

It talks only to your own OpenTodo server. It needs no extra Python packages and sends nothing anywhere else.

## What you get

- **To-do lists**: the Inbox, Today and one list per project. Add, rename, complete, reopen, reschedule, reorder and delete tasks from Home Assistant. Text you add is read with OpenTodo's quick-add syntax, so `milk tomorrow #Home p1 @errands` becomes the task "milk" in Home, due tomorrow, priority 1, labelled errands.
- **Counts**: Today, Earlier (the late tasks at the top of Today), the Inbox, and each project's open tasks, each with the first tasks as an attribute.
- **Calendar**: tasks with a do-on date. A task with a time is a block of its estimate (30 minutes without one).
- **Events**: `opentodo_task_created`, `opentodo_task_updated`, `opentodo_task_completed`, `opentodo_task_reopened`, `opentodo_task_deleted`, `opentodo_task_assigned` and `opentodo_task_due`.
- **Action**: `opentodo.add_task` adds a task (quick-add syntax, optional project, due date, priority, labels and notes) and returns it.
- **Assist**: "add bread to the Home list" and "check off bread from the Home list" work through Home Assistant's own list intents.
- **Instant updates**: the integration creates an OpenTodo webhook that calls Home Assistant, so changes arrive within a second. Without it, it checks every few minutes.

## Install

Through [HACS](https://hacs.xyz):

1. In HACS, open the menu, choose **Custom repositories**, add `https://github.com/OpenTodoApp/opentodo-homeassistant` with the type **Integration**.
2. Find **OpenTodo** in HACS and download it.
3. Restart Home Assistant.

By hand: copy `custom_components/opentodo` into the `custom_components` folder of your Home Assistant configuration and restart.

It needs Home Assistant 2026.3 or later and OpenTodo 0.12 or later.

## Set up

1. In OpenTodo, open **Settings → Integrations → API tokens**, choose **New token**, name it "Home Assistant" and give it **Full access**. Copy the token; it is shown once.
2. In Home Assistant, open **Settings → Devices & services → Add integration → OpenTodo**.
3. Enter the address you open OpenTodo at (for example `http://192.168.1.20:8080`) and the token.

Which token:

| Access | What works |
| --- | --- |
| **Full access** (recommended) | Everything: lists you can change, counts, calendar, events, instant updates. |
| **Read only** | Counts, calendar, events (by polling) and lists you can only look at. |
| AI assistant (agent) | Refused: it can make only a few kinds of changes, and records them as an assistant's. |
| Admin | Refused: Home Assistant does not need access to credentials and administration. |

An account with end-to-end encryption cannot be used: the server cannot read task titles, so neither can Home Assistant. The setup says so, and a repair issue appears if encryption is turned on later.

## Instant updates

When the integration starts, it creates one webhook in OpenTodo that points at Home Assistant's address and checks it every hour (it re-enables it if OpenTodo disabled it after failures, and follows a new address). Each delivery is signed; Home Assistant checks the signature and the time before it accepts it.

OpenTodo refuses webhooks to private network addresses unless its owner allows them. If Home Assistant runs on your LAN, the owner turns on **Settings → Integrations → Webhooks → Allow private network targets** in OpenTodo. Until then the integration polls and shows a repair issue explaining this.

If OpenTodo cannot reach Home Assistant at the address Home Assistant knows itself by (for example because they run in different Docker networks), set **Home Assistant address for OpenTodo** in the integration's options.

## Options

| Option | Default | Meaning |
| --- | --- | --- |
| Show every project | on | A list and a count for each project, new ones included. Off: choose projects. |
| Show a Today list | on | The app's Today: due today or earlier, a deadline reached, or a start date come. |
| Keep completed tasks visible | 1 day | 0 hides completed tasks. |
| Read quick-add syntax | on | Off: the text you add is the title as it is. |
| Check for changes every | 2 minutes | With instant updates working, at least every 15 minutes. |
| Instant updates | on | The webhook described above. |
| Home Assistant address for OpenTodo | empty | Only when the usual address does not work. |

## Events

Every event carries `task_id`, `title`, `description`, `project_id`, `project_name`, `due` (ISO date or date-time in Home Assistant's time zone), `deadline`, `priority`, `labels` (names), `assignee_id`, `completed_at`, `completed_by`, `series_id`, `url` (opens the task in OpenTodo), `delivery` (`push`, `poll` or `clock`), `source` (who changed it, from the webhook: `device`, `server`, `caldav`, `rule`, …) and, for changes, `changes` (`{field: {from, to}}`).

- Completing a repeating task fires `opentodo_task_completed` for the completed occurrence (its `series_id` is the repeating task).
- `opentodo_task_assigned` fires with `created` or `updated` when a task gets an assignee.
- `opentodo_task_due` fires when a timed task's time comes, and at the start of the day for a task with a date only (`all_day: true`).

```yaml
alias: Plants watered
triggers:
  - trigger: event
    event_type: opentodo_task_completed
    event_data:
      title: Water plants
actions:
  - action: notify.mobile_app_phone
    data:
      message: "{{ trigger.event.data.title }} is done"
```

## Limits

- Lists are flat: subtasks are listed after their parent.
- Removing an item, or "clear completed", moves tasks to OpenTodo's Trash, where they can be restored.
- Dates without a time zone are read in Home Assistant's time zone. The app's "day starts at" preference is not applied.
- The calendar is read-only and shows each repeating task's next occurrence only.
- A shared project where you are a viewer shows its tasks, but changes are refused with OpenTodo's message.

The [OpenTodo guide](https://github.com/OpenTodoApp/OpenTodo/blob/main/docs/home-assistant.md) has more, with automation examples.

## Development

```bash
uv venv -p 3.14 .venv
uv pip install -p .venv/bin/python -r requirements_test.txt
.venv/bin/python -m pytest tests --cov
python tests_e2e/e2e.py   # real OpenTodo and Home Assistant containers; needs Docker
```

## Licence

AGPL-3.0, like OpenTodo. See [LICENSE](LICENSE).
