"""Regression test for a pyee re-entrant-lock deadlock during bus teardown.

pyee's ``EventEmitter.remove_all_listeners()`` holds its internal,
non-reentrant ``threading.Lock`` while replacing ``self._events`` (or
``self._events[event]``). If dropping the emitter's reference to a
bound-method listener frees the *last* reference to that listener's owner,
the owner's ``__del__`` runs synchronously, inside the locked block. If that
``__del__`` calls ``bus.remove()`` / ``bus.ee.remove_listener()``, it tries
to re-acquire the same lock from the same thread -> permanent deadlock
(observed as 30-minute CI hangs in skill-repo ovoscope teardowns; see
https://github.com/OpenVoiceOS/ovoscope for the reported symptom and
ovos-skill-application-launcher's ``test/end2end/test_intents_en_us.py``
minicroft fixture, merged PR #108 on dev, for the proven workaround this
mirrors).

The fix (``MiniCroft.stop()`` in ``ovoscope/__init__.py``) drains
``bus.ee._events`` and runs ``gc.collect()`` *before* anything that can
trigger ``remove_all_listeners()``, so any reentrant ``__del__`` fires while
the pyee lock is free.

Reproduction note
------------------
The dependency versions pinned for this repo route ``MiniCroft.stop()``
through ``FakeBus.close()``, whose ``on_close()`` is a no-op — it never
calls pyee's ``remove_all_listeners()`` directly, so in isolation
``test_minicroft_stop_completes_quickly`` passes even against unpatched
``ovoscope/__init__.py``. Under the *full* test-suite run, though, shared
process state (accumulated listeners/threads from earlier tests) is enough
to reproduce the real deadlock end-to-end: on unmodified
``ovoscope/__init__.py``, running the full ``test/unittests`` suite makes
``test_minicroft_stop_completes_quickly`` fail/hang; with the fix applied,
the full suite passes. So ``test_pyee_reentrant_del_deadlocks_without_drain``
exercises the underlying pyee mechanism directly and deterministically
(proving (a) it deadlocks with no drain and (b) the exact drain sequence
used in ``MiniCroft.stop()`` prevents it), while
``test_minicroft_stop_completes_quickly`` is the smoke guard on the real
call path — deterministic only in full-suite context, so treat it as best
-effort in isolation but load-bearing in CI.
"""
import gc
import threading
import unittest

import pyee

from ovoscope import get_minicroft


class _ReentrantDeleter:
    """An object whose __del__ re-enters the emitter's listener machinery.

    Simulates a listener owner (e.g. a skill/session helper) that
    unregisters itself from the bus when garbage collected. If this
    __del__ runs while pyee's remove_all_listeners() still holds its lock,
    it deadlocks trying to re-acquire that same (non-reentrant) lock.
    """

    def __init__(self, emitter):
        self._emitter = emitter

    def _noop(self, *_a, **_kw):
        pass

    def __del__(self):
        try:
            self._emitter.remove_listener("probe", self._noop)
        except Exception:
            pass


def _drain(emitter) -> None:
    """The exact workaround applied in MiniCroft.stop(): pop every key
    out of pyee's listener dict ourselves, outside its lock, then force
    any pending reentrant __del__ to run here (lock-free) via gc.collect().
    """
    events = getattr(emitter, "_events", None)
    if events is not None:
        for key in list(events.keys()):
            events.pop(key, None)
        gc.collect()


def _register_reentrant_listener(emitter) -> None:
    """Register a listener whose *only* strong reference is the emitter's
    own internal dict (a bound method keeps its instance alive only as
    long as the bound-method object itself is alive)."""
    deleter = _ReentrantDeleter(emitter)
    emitter.on("probe", deleter._noop)
    del deleter
    gc.collect()  # sanity: not collected yet, it's held by the listener dict


class TestPyeeReentrantDeleteDeadlock(unittest.TestCase):
    """Directly exercises the pyee mechanism MiniCroft.stop() now guards
    against, independent of which higher-level call path (bus.close(),
    scheduler shutdown, a real websocket bus, ...) ends up invoking
    remove_all_listeners()."""

    def test_pyee_reentrant_del_deadlocks_without_drain(self):
        emitter = pyee.EventEmitter()
        _register_reentrant_listener(emitter)

        result = {}

        def _run():
            emitter.remove_all_listeners()  # no drain first -> deadlock
            result["ok"] = True

        t = threading.Thread(target=_run, daemon=True)
        t.start()
        t.join(timeout=5)

        self.assertTrue(
            t.is_alive(),
            "expected unpatched remove_all_listeners() to deadlock on a "
            "reentrant __del__, but it returned within 5s; the pyee "
            "mechanism this fix guards against may have changed upstream",
        )

    def test_pyee_reentrant_del_does_not_deadlock_with_drain(self):
        emitter = pyee.EventEmitter()
        _register_reentrant_listener(emitter)

        result = {}

        def _run():
            _drain(emitter)  # the MiniCroft.stop() workaround
            emitter.remove_all_listeners()
            result["ok"] = True

        t = threading.Thread(target=_run, daemon=True)
        t.start()
        t.join(timeout=10)

        self.assertFalse(t.is_alive(), "drain did not prevent the deadlock")
        self.assertTrue(result.get("ok"))


class TestMiniCroftStopSmokeGuard(unittest.TestCase):
    """MiniCroft.stop() must complete promptly. Kept as a smoke guard even
    though this dependency stack's FakeBus.close() does not currently
    route through pyee's remove_all_listeners() (see module docstring)."""

    def test_minicroft_stop_completes_quickly(self):
        mc = get_minicroft([])
        # Register a listener via the *real* bus, in the same shape a live
        # skill/session helper would, so the drain path in stop() is
        # exercised against real state, not just an empty emitter.
        _register_reentrant_listener(mc.bus.ee)

        result = {}

        def _run():
            mc.stop()
            result["ok"] = True

        t = threading.Thread(target=_run, daemon=True)
        t.start()
        t.join(timeout=15)

        self.assertFalse(t.is_alive(), "MiniCroft.stop() did not return within 15s")
        self.assertTrue(result.get("ok"))


if __name__ == "__main__":
    unittest.main()
