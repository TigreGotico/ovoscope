# GUI Testing

`GUICaptureSession` captures the `gui.*` and `mycroft.gui.*` bus messages emitted
during a skill interaction, so tests can assert page navigation, namespace values,
and namespace teardown without cluttering the main message capture.

## Why GUI Messages Are Separate

`End2EndTest` filters `gui.*` messages out by default (`ignore_gui=True`). This is
deliberate: GUI namespace churn (``gui.value.set``, ``gui.clear.namespace``) is
high-frequency and rarely the focus of intent/dialogue tests. `GUICaptureSession`
provides a complementary, opt-in capture layer for tests that *do* care about GUI
state.

## Quick Start

```python
from ovoscope import get_minicroft, GUICaptureSession
from ovos_bus_client.message import Message

mc = get_minicroft(["ovos-skill-hello-world.openvoiceos"])

with GUICaptureSession(mc.bus) as gui:
    mc.bus.emit(Message(
        "recognizer_loop:utterance",
        data={"utterances": ["hello"], "lang": "en-US"},
    ))
    import time; time.sleep(2)
    gui.assert_page_shown("helloworldskill", "hello.qml")

mc.stop()
```

`GUICaptureSession` can also be used alongside `End2EndTest`. Run
`End2EndTest.execute()` inside the `with GUICaptureSession(...)` block:

```python
from ovoscope import get_minicroft, End2EndTest, GUICaptureSession
from ovos_bus_client.message import Message
from ovos_bus_client.session import Session

mc = get_minicroft(["ovos-skill-hello-world.openvoiceos"])
session = Session("test-gui-1")
utterance = Message(
    "recognizer_loop:utterance",
    {"utterances": ["hello"], "lang": "en-US"},
    {"session": session.serialize(), "source": "A", "destination": "B"},
)

with GUICaptureSession(mc.bus) as gui:
    End2EndTest(
        skill_ids=[],          # skill already loaded in mc
        source_message=utterance,
        expected_messages=[
            utterance,
            Message("speak", {"utterance": "Hello!"}),
            Message("ovos.utterance.handled", {}),
        ],
        minicroft=mc,
    ).execute()
    gui.assert_page_shown("helloworldskill", "hello.qml")

mc.stop()
```

## Class: `GUICaptureSession`

`GUICaptureSession`: `ovoscope/__init__.py`

```python
from ovoscope import GUICaptureSession
```

A `dataclass` and context manager. Subscribe it to a `FakeBus` to start
recording GUI-prefixed messages.

### Constructor

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `bus` | `Any` | **required** | The `FakeBus` to subscribe to. Typically `mc.bus`. |
| `prefixes` | `List[str]` | `["gui.", "mycroft.gui."]` | Message-type prefixes to capture. All messages whose `msg_type` starts with any prefix are recorded. |

### Attributes

| Attribute | Type | Description |
|-----------|------|-------------|
| `messages` | `List[Message]` | Accumulated GUI messages captured since `start()`. |

### Lifecycle Methods

`GUICaptureSession.start`: `ovoscope/__init__.py`

```python
gui = GUICaptureSession(mc.bus)
gui.start()
# ... interaction ...
gui.stop()
```

| Method | Description |
|--------|-------------|
| `start()` | Subscribe to the bus and begin capturing. |
| `stop()` | Unsubscribe from the bus and stop capturing. |

`GUICaptureSession.__enter__` / `__exit__`: `ovoscope/__init__.py`

The preferred usage is as a context manager. `__enter__` calls `start()`;
`__exit__` calls `stop()`.

### Assertion Methods

#### `assert_page_shown(namespace, page, timeout=2.0, exact=True)`

`GUICaptureSession.assert_page_shown`: `ovoscope/__init__.py`

Assert that a `gui.page.show` (or equivalent) message was emitted for the
given namespace and page filename.

```python
gui.assert_page_shown("helloworldskill", "hello.qml", timeout=3.0)
```

| Argument | Type | Default | Description |
|----------|------|---------|-------------|
| `namespace` | `str` | **required** | GUI namespace (typically the skill ID slug, e.g. `"helloworldskill"`). |
| `page` | `str` | **required** | QML page filename (e.g. `"hello.qml"`). |
| `timeout` | `float` | `2.0` | Max seconds to poll captured messages before failing. |
| `exact` | `bool` | `True` | Exact matching: the namespace must be equal, and the page must equal the shown page's basename. Pass `exact=False` for the legacy substring behavior. |

Raises `AssertionError` if no matching message is found within `timeout`.

The method checks both `msg.data["namespace"]` / `msg.context["skill_id"]`
for the namespace, and `msg.data["pages"]` / `msg.data["page"]` for the
page name. By default both use exact matching (`namespace ==`, page
basename `==`), so a near-miss like `hello.qml.bak` or a longer namespace
that merely contains yours cannot satisfy the assertion.

#### `assert_namespace_value(namespace, key, value, exact=True)`

`GUICaptureSession.assert_namespace_value`: `ovoscope/__init__.py`

Assert that a `gui.value.set` or `gui.namespace.update` message set a
specific key to a specific value in the given namespace.

```python
gui.assert_namespace_value("helloworldskill", "greeting", "Hello!")
```

| Argument | Type | Description |
|----------|------|-------------|
| `namespace` | `str` | GUI namespace to check. |
| `key` | `str` | Data key within the namespace. |
| `value` | `Any` | Expected value (exact equality). |

Raises `AssertionError` if no matching message is found.

#### `assert_namespace_has_key(namespace, key, exact=True)`

`GUICaptureSession.assert_namespace_has_key`: `ovoscope/__init__.py`

Assert that a `gui.value.set` or `gui.namespace.update` message set a
specific key in the given namespace, regardless of value. Useful for
dynamic data (weather API responses, timestamps) where the exact value
is unpredictable.

```python
gui.assert_namespace_has_key("weatherskill", "current_temp")
```

| Argument | Type | Description |
|----------|------|-------------|
| `namespace` | `str` | GUI namespace to check. |
| `key` | `str` | Data key that should exist. |

Raises `AssertionError` if no matching message is found.

#### `assert_namespace_cleared(namespace, exact=True)`

`GUICaptureSession.assert_namespace_cleared`: `ovoscope/__init__.py`

Assert that a `gui.namespace.remove`, `gui.namespace.clear`, or
`gui.clear.namespace` message was emitted for the given namespace —
`gui.clear.namespace` is the topic the GUI service actually emits at
runtime. The namespace comparison is exact unless `exact=False`.

```python
gui.assert_namespace_cleared("helloworldskill")
```

Raises `AssertionError` if no matching message is found.

## Message Filtering

Only messages whose `msg_type` starts with one of the configured `prefixes`
are captured: `GUICaptureSession._on_message`: `ovoscope/__init__.py`.
All other bus messages are ignored.

Default captured message types (partial list):

| Message Type | Meaning |
|-------------|---------|
| `gui.page.show` | Skill requested a page be displayed |
| `gui.value.set` | Skill updated a namespace key |
| `gui.clear.namespace` | Skill cleared its GUI namespace |
| `mycroft.gui.screen.close` | GUI screen close request |

## Combining with `End2EndTest`

The recommended pattern is to run `End2EndTest.execute()` inside a
`GUICaptureSession` context manager so both ordered dialogue and GUI
messages are captured in a single interaction:

```python
with GUICaptureSession(mc.bus) as gui:
    test = End2EndTest(
        skill_ids=[],
        minicroft=mc,
        source_message=utterance,
        expected_messages=[...],
        ignore_gui=True,   # default: keeps End2EndTest clean
    )
    test.execute()
    # Now assert GUI state separately
    gui.assert_page_shown("my_skill", "main.qml")
    gui.assert_namespace_value("my_skill", "title", "My Page")
```

Setting `ignore_gui=True` (the default on `End2EndTest`) keeps the ordered
message sequence clean while `GUICaptureSession` captures the GUI events
independently.

## Template-Based GUI (`SYSTEM_*` templates)

Skills that use the typed template API (`self.gui.show_weather(...)`,
`show_text(...)`, `show_list(...)`, …) emit a `gui.page.show` for a built-in
`SYSTEM_*` template plus `gui.value.set` for its data keys. `assert_template_shown`
checks both in one call:

```python
with GUICaptureSession(mc.bus) as gui:
    mc.bus.emit(Message("recognizer_loop:utterance",
                        {"utterances": ["what's the weather"], "lang": "en-US"}))
    import time; time.sleep(2)
    # the SYSTEM_ prefix is optional ("weather" == "SYSTEM_weather")
    gui.assert_template_shown(
        "ovos-skill-weather.openvoiceos",
        "weather",
        values={"current_temp": 22, "condition": "Sunny"},
    )
```

This is the recommended assertion for the template-based GUI: it does not care
which display backend (Qt, pyhtmx, …) renders the template: only that the skill
requested the right `SYSTEM_*` template with the right session data.

## What `GUICaptureSession` Does NOT Cover

- Full GUI rendering: only bus messages are captured; no QML engine is run.
- `ovos-gui` service behaviour: only the `FakeBus` in-process messages are
  captured; messages sent to a real GUI over WebSocket are not included.
- GUI framework events not prefixed with `gui.` or `mycroft.gui.` (these can
  be added via the `prefixes` constructor argument).

## Cross-References

- `CaptureSession`: `ovoscope/docs/capture-session.md` (ordered dialogue capture)
- `End2EndTest`: `ovoscope/docs/end2end-test.md` (full test runner)
- `MiniCroft` / `get_minicroft()`: `ovoscope/docs/minicroft.md`
- `GUI_IGNORED` message list: `ovoscope/__init__.py`

---
[← Voice Loop](voice-loop.md) · [Home](../README.md) · [Bus Coverage →](bus-coverage.md)
