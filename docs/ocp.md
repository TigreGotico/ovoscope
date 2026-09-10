# OCP / Common Play Testing

`ovoscope.ocp` provides `OCPTest` and `assert_ocp_query_response` for testing
OCP (OpenVoiceOS Common Play) skills that handle media queries.  For testing
the OCP player state machine, see `OCPPlayerHarness` in `ovoscope.media`.

## OCP Message Flow

```
recognizer_loop:utterance
  → ovos.common_play.query              (broadcast to all OCP skills)
  → ovos.common_play.query.response     (skill replies with MediaEntry list)
  → ovos.common_play.start              (selected track)
```

## `OCPTest`: Declarative Style

`OCPTest` (`ocp.py:OCPTest`)

```python
from ovoscope.ocp import OCPTest

result = OCPTest(
    skill_ids=["ovos-skill-youtube.openvoiceos"],
    utterance="play lofi hip hop",
    mock_responses={
        "youtube.com": {"items": [{"title": "Lofi Radio", "url": "..."}]},
    },
    expected_media=[{"title": "Lofi Radio"}],
    lang="en-US",
    timeout=20.0,
).execute()
```

### Fields

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `skill_ids` | `List[str]` | **required** | OCP skill IDs to load. |
| `utterance` | `str` | **required** | User utterance. |
| `mock_responses` | `Dict[str, Any]` | `{}` | URL-substring → JSON response body. |
| `expected_media` | `List[Dict]` | `[]` | Partial dicts; each must match one `media_list` item. |
| `expected_stream_url` | `Optional[str]` | `None` | Substring expected in `ovos.common_play.start` URI. |
| `lang` | `str` | `"en-US"` | Language tag. |
| `timeout` | `float` | `20.0` | Max wait in seconds. |
| `patch_targets` | `List[str]` | `[]` | Additional `requests`-like module paths to patch (dotted Python path to the callable to replace). |

### `execute()`: `ovoscope/ocp.py`

Returns `List[Message]`: all bus messages captured during the interaction
(same format as `CaptureSession.responses`).

## HTTP Mocking: `ovoscope/ocp.py`

HTTP calls are intercepted via `unittest.mock.patch` on `requests.Session.get`
and `requests.get` by default.

The `mock_responses` dict maps **URL substrings** to JSON response bodies.
When the patched `get()` is called, the mock checks if any key is a substring
of the request URL and returns the corresponding body.

For skills using non-standard HTTP clients (e.g. `aiohttp`, `httpx`), pass
additional dotted Python module paths in `patch_targets`. The path must point
to the exact callable that the skill imports and calls:

```python
# Default: patches requests.Session.get and requests.get automatically.
# Use patch_targets for any other HTTP client the skill uses.

OCPTest(
    skill_ids=["ovos-skill-example-aiohttp.openvoiceos"],
    utterance="play jazz",
    mock_responses={
        "api.example.com": {"results": [{"title": "Jazz Radio", "url": "http://stream.example.com/jazz"}]},
    },
    # Dotted path: <module-where-the-symbol-is-used>.<callable>
    patch_targets=["ovos_skill_example.api_client.aiohttp.ClientSession.get"],
).execute()
```

The format is the same as `unittest.mock.patch` target strings: the dotted
path to where the symbol is **used** (not where it is defined). See
[unittest.mock patch docs](https://docs.python.org/3/library/unittest.mock.html#unittest.mock.patch)
for details.

## `assert_ocp_query_response`

`assert_ocp_query_response` (`ocp.py:assert_ocp_query_response`)

```python
from ovoscope.ocp import assert_ocp_query_response

assert_ocp_query_response(
    messages,
    min_results=1,
    media_type="audio",
    expected_media=[{"title": "My Song"}],
    stream_url_contains="cdn.example.com",
)
```

| Argument | Description |
|----------|-------------|
| `messages` | Captured message list. |
| `min_results` | Minimum `media_list` length. |
| `media_type` | All items must have this `media_type`. |
| `expected_media` | Partial-dict subset matching. |
| `stream_url_contains` | Substring in `ovos.common_play.start` URI. |

---
[← Media Provider Testing](media-provider-testing.md) · [Home](../README.md) · [PHAL →](phal.md)
