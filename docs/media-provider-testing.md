# MediaProvider (Search) Testing with ovoscope

`MediaProviderHarness` (`ovoscope.media_provider`) tests `opm.media.provider`
plugins: the in-process **catalog/search** providers introduced by the
ovos-media sprint to replace OCP search skills. It is the search-side counterpart
to [`OCPPlayerHarness`](media-testing.md), which drives the *player*.

> **No extra required.** Unlike the player harness, `MediaProviderHarness` is
> **duck-typed**: it imports neither `mediavocab` nor `ovos-plugin-manager`'s
> `MediaProvider` / `QueryContext`. Your *test* supplies the `Signals` /
> `QueryContext` objects, so ovoscope itself stays dependency-free. The provider
> package and `mediavocab` only need to be installed for the test that uses it.

## Why a dedicated harness

There is no released loader for the `opm.media.provider` entry-point group, and
the OCP pipeline does not yet load providers in-process. So the harness models the
pipeline's *intended* path and removes the boilerplate every provider e2e would
otherwise repeat:

```
discover the entry-point  ->  serves(signals, context) gate  ->  search_safe()
```

## Basic usage

```python
from ovoscope import MediaProviderHarness
from mediavocab import MediaType, Signals
from ovos_plugin_manager.templates.media_provider import QueryContext

h = MediaProviderHarness.from_entrypoint(
    "music_assistant",                       # opm.media.provider entry-point name
    config={"url": "http://mass.local:8095"},
    mock_api=my_mock_client,                 # injected onto provider._api
)

# packaging registered the plugin
h.assert_entrypoint_registered()

# three-axis + context routing gate
h.assert_routes(Signals(medium=MediaType.MUSIC),
                QueryContext(supported_playback_types={"audio"}))
h.assert_not_routes(Signals(medium=MediaType.MUSIC),
                    QueryContext(supported_playback_types={"video"}))  # audio-only provider
h.assert_not_routes(Signals(medium=MediaType.MOVIE))

# the never-raising search the pipeline calls: ranked, playable results
releases = h.assert_returns_playables(Signals(title="worms"))
assert all(r.uri.startswith("library://") for r in releases)
```

## Constructing the harness

| Constructor | Use |
|---|---|
| `MediaProviderHarness.from_entrypoint(name, config=None, group="opm.media.provider", mock_api=None, api_attr="_api")` | Discover the provider through its installed entry-point (the real e2e). Raises `AssertionError` if the entry-point is missing or ambiguous. |
| `MediaProviderHarness.from_class(provider_cls, config=None, mock_api=None, api_attr="_api")` | Wrap a class you already hold (no packaging needed): handy for unit tests. |

`mock_api` is set onto `provider.<api_attr>` (default `_api`) to bypass the lazy,
network-backed client a provider builds on first use.

## Drivers

Thin pass-throughs mirroring the `MediaProvider` contract:

| Method | Delegates to |
|---|---|
| `is_available()` | `provider.is_available()` |
| `serves(signals, context=None)` | `provider.serves(...)` |
| `search(signals, lang="en-us")` | `provider.search(...)` (may raise) |
| `search_safe(signals, context=None, lang="en-us")` | `provider.search_safe(...)` (never raises) |
| `featured_media(lang="en-us")` | `provider.featured_media(...)` |

## Assertions

| Method | Checks |
|---|---|
| `assert_entrypoint_registered(name=None, group=None)` | the provider is discoverable under its entry-point group |
| `assert_routes(signals, context=None)` | `serves(...)` is `True` |
| `assert_not_routes(signals, context=None)` | `serves(...)` is `False` |
| `assert_returns_playables(signals, context=None, lang="en-us")` | `search_safe` returns results, each with a truthy `uri`, a `match_confidence` in `[0,1]`, and a `work`; returns the results |

## Exposed attributes

| Attribute | Description |
|---|---|
| `provider` | the wrapped provider instance |
| `api` | the injected mock client (for custom `assert_called_with` checks) |
| `entrypoint_name` / `entrypoint_group` | set when built via `from_entrypoint` |

## Cross-references

- `MediaProvider` / `QueryContext` (`ovos_plugin_manager.templates.media_provider`)
- `Signals`, `Release`, `MediaType` (`mediavocab`)
- Player-side harness: [media-testing.md](media-testing.md)
- OCP *search skill* testing (legacy stack): [ocp.md](ocp.md)

---
[← Media Testing](media-testing.md) · [Home](../README.md) · [OCP →](ocp.md)
