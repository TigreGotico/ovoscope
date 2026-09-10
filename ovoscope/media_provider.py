# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#    http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""MediaProvider (catalog/search) test harness for ovoscope.

``ovoscope.media`` drives the OCP *player* state-machine; this module is its
catalog/search counterpart — a harness for ``opm.media.provider`` plugins
(``MediaProvider`` subclasses introduced by the ovos-media sprint).

There is no released loader for the ``opm.media.provider`` entry-point group, and
the OCP pipeline does not yet load providers in-process, so this harness models
the pipeline's *intended* path:

    discover the provider  ->  gate it with ``serves(signals, context)``
                           ->  call the never-raising ``search_safe``

It is deliberately **duck-typed**: it imports neither mediavocab (``Signals`` /
``Release``) nor ovos-plugin-manager's ``MediaProvider`` / ``QueryContext``. The
test supplies whatever ``signals`` / ``context`` objects the provider expects, so
ovoscope stays dependency-free and usable even without the (currently branch-only)
``opm.media.provider`` plugin type installed.

Usage::

    from ovoscope import MediaProviderHarness

    h = MediaProviderHarness.from_entrypoint(
        "music_assistant",
        config={"url": "http://mass.local:8095"},
        mock_api=my_mock_client,            # injected onto provider._api
    )
    h.assert_entrypoint_registered()
    h.assert_routes(Signals(medium=MediaType.MUSIC),
                    QueryContext(supported_playback_types={"audio"}))
    h.assert_not_routes(Signals(medium=MediaType.MOVIE))
    releases = h.assert_returns_playables(Signals(title="worms"))
"""
from __future__ import annotations

import concurrent.futures
from importlib.metadata import entry_points
from typing import Any, Callable, List, Optional

DEFAULT_GROUP = "opm.media.provider"
DEFAULT_CALL_TIMEOUT = 30.0


class MediaProviderHarness:
    """Wrap a ``MediaProvider`` instance and drive it the way the OCP pipeline does.

    Build one with :meth:`from_entrypoint` (real plugin discovery) or
    :meth:`from_class` (a class you already hold — used by ovoscope's own tests).
    The wrapped instance is exposed as :attr:`provider` and the injected mock
    client as :attr:`api` for custom assertions.
    """

    def __init__(self, provider: Any, api: Any = None,
                 entrypoint_name: Optional[str] = None,
                 entrypoint_group: str = DEFAULT_GROUP,
                 call_timeout: Optional[float] = DEFAULT_CALL_TIMEOUT) -> None:
        self.provider = provider
        self.api = api
        self.entrypoint_name = entrypoint_name
        self.entrypoint_group = entrypoint_group
        # A real provider talks to the network. Without a deadline a hung
        # server (no socket timeout of its own) blocks the test run forever
        # instead of failing it. Set to None to wait indefinitely.
        self.call_timeout = call_timeout

    def _call(self, func: Callable, *args, **kwargs) -> Any:
        """Run a provider call under :attr:`call_timeout`.

        Raises:
            TimeoutError: when the provider does not answer in time.
        """
        if self.call_timeout is None:
            return func(*args, **kwargs)
        # NOT a `with` block: ThreadPoolExecutor.__exit__ shuts down with
        # wait=True, which would block on the very call that timed out.
        pool = concurrent.futures.ThreadPoolExecutor(max_workers=1)
        try:
            future = pool.submit(func, *args, **kwargs)
            try:
                return future.result(timeout=self.call_timeout)
            except concurrent.futures.TimeoutError:
                raise TimeoutError(
                    f"provider {type(self.provider).__name__}."
                    f"{getattr(func, '__name__', func)}() did not answer "
                    f"within {self.call_timeout}s"
                ) from None
        finally:
            pool.shutdown(wait=False)

    # ------------------------------------------------------------------
    # Constructors
    # ------------------------------------------------------------------

    @classmethod
    def from_class(cls, provider_cls: Any, config: Optional[dict] = None,
                   mock_api: Any = None, api_attr: str = "_api",
                   call_timeout: Optional[float] = DEFAULT_CALL_TIMEOUT
                   ) -> "MediaProviderHarness":
        """Instantiate ``provider_cls(config)`` and (optionally) inject ``mock_api``.

        Args:
            provider_cls: a ``MediaProvider`` subclass (duck-typed — anything with
                ``is_available`` / ``serves`` / ``search`` / ``search_safe`` /
                ``featured_media``).
            config: config dict passed to the provider constructor.
            mock_api: object set onto ``provider.<api_attr>`` to bypass the real
                (network) client built lazily by the provider.
            api_attr: attribute name the provider reads its client from
                (default ``"_api"``).
        """
        provider = provider_cls(config or {})
        if mock_api is not None:
            setattr(provider, api_attr, mock_api)
        return cls(provider, api=mock_api, call_timeout=call_timeout)

    @classmethod
    def from_entrypoint(cls, name: str, config: Optional[dict] = None,
                        group: str = DEFAULT_GROUP, mock_api: Any = None,
                        api_attr: str = "_api",
                        call_timeout: Optional[float] = DEFAULT_CALL_TIMEOUT
                        ) -> "MediaProviderHarness":
        """Discover the provider through its installed entry-point and wrap it.

        Resolves ``name`` in the ``group`` entry-point group (default
        ``opm.media.provider``), loads the class, and delegates to
        :meth:`from_class`. Raises ``AssertionError`` if the entry-point is not
        installed (or is ambiguous) — that *is* the e2e signal that the plugin's
        packaging registered it correctly.
        """
        matches = [ep for ep in entry_points(group=group) if ep.name == name]
        if not matches:
            raise AssertionError(
                f"no {group!r} entry-point named {name!r} is installed — "
                f"is the provider package installed (pip install -e .)?"
            )
        if len(matches) > 1:
            raise AssertionError(
                f"multiple {group!r} entry-points named {name!r}: {matches!r}"
            )
        provider_cls = matches[0].load()
        harness = cls.from_class(provider_cls, config=config,
                                 mock_api=mock_api, api_attr=api_attr,
                                 call_timeout=call_timeout)
        harness.entrypoint_name = name
        harness.entrypoint_group = group
        return harness

    # ------------------------------------------------------------------
    # Drivers — mirror the pipeline's discover -> gate -> search path
    # ------------------------------------------------------------------

    def is_available(self) -> bool:
        """Provider self-check (server reachable / keys present)."""
        return self._call(self.provider.is_available)

    def serves(self, signals: Any, context: Any = None) -> bool:
        """Context-aware routing gate (three-axis ``matches`` + device/policy)."""
        return self._call(self.provider.serves, signals, context)

    def search(self, signals: Any, lang: str = "en-us") -> List[Any]:
        """Raw search — may raise, mirroring a direct provider call."""
        return self._call(self.provider.search, signals, lang=lang)

    def search_safe(self, signals: Any, context: Any = None,
                    lang: str = "en-us") -> List[Any]:
        """The never-raising entry the pipeline's thread-pool dispatch calls."""
        return self._call(self.provider.search_safe, signals,
                          context=context, lang=lang)

    def featured_media(self, lang: str = "en-us") -> List[Any]:
        """Curated/home content (recently-played, recommendations, …)."""
        return self._call(self.provider.featured_media, lang=lang)

    # ------------------------------------------------------------------
    # Assertions
    # ------------------------------------------------------------------

    def assert_entrypoint_registered(self, name: Optional[str] = None,
                                     group: Optional[str] = None) -> None:
        """Assert the provider is discoverable under its entry-point group."""
        name = name or self.entrypoint_name
        group = group or self.entrypoint_group
        assert name, ("no entry-point name to check — pass name=, or build the "
                      "harness with from_entrypoint()")
        names = [ep.name for ep in entry_points(group=group)]
        assert name in names, (
            f"{name!r} is not registered under {group!r}; found {names!r}"
        )

    def assert_routes(self, signals: Any, context: Any = None) -> None:
        """Assert the provider serves ``signals`` under ``context``."""
        assert self.serves(signals, context), (
            f"provider does not serve {signals!r} (context={context!r})"
        )

    def assert_not_routes(self, signals: Any, context: Any = None) -> None:
        """Assert the provider is gated out for ``signals`` under ``context``."""
        assert not self.serves(signals, context), (
            f"provider unexpectedly serves {signals!r} (context={context!r})"
        )

    def assert_returns_playables(self, signals: Any, context: Any = None,
                                 lang: str = "en-us") -> List[Any]:
        """Assert ``search_safe`` returns ranked, playable results and return them.

        Each result must carry a truthy ``uri``, a ``match_confidence`` in
        ``[0.0, 1.0]``, and a ``work``.
        """
        results = self.search_safe(signals, context=context, lang=lang)
        assert results, f"provider returned no results for {signals!r}"
        for r in results:
            assert getattr(r, "uri", None), f"result has no uri: {r!r}"
            mc = getattr(r, "match_confidence", None)
            assert mc is not None and 0.0 <= mc <= 1.0, (
                f"result match_confidence out of [0,1]: {mc!r} ({r!r})"
            )
            assert getattr(r, "work", None) is not None, f"result has no work: {r!r}"
        return results
