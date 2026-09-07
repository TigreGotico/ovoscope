"""Golden EFFECT test for the m2v-multilingual boot recipe.

Boots ovos-skill-personal through the candidate default intent engine — the
model2vec multilingual classifier — via ``get_m2v_minicroft`` and asserts, for
each utterance, the whole round trip: the correct intent routed AND the
rendered spoken dialog carries the real answer words (or the slot value), not
the dialog file name. One utterance is Spanish, proving the multilingual model
routes localized input.

The skill is loaded under its real registered id
(``ovos-skill-personal.openvoiceos``) through the normal skill-plugin install:
the classifier's trained labels carry the author-qualified skill id, so the
model's classes intersect the labels the skill registers at runtime and route
directly. ``test_registered_skill_id_labels_route`` pins that the model's class
set carries the ``.openvoiceos`` labels and that they intersect the loaded
skill's registered intents.

This is a live test: the ~512MB model downloads to the shared HuggingFace cache
on first run. It is skipped unless OVOSCOPE_LIVE=1 and the m2v pipeline is
installed, mirroring ovos-m2v-pipeline's own live fixture.
"""
import os
import shutil
import tempfile
import unittest

from ovos_bus_client.message import Message
from ovos_bus_client.session import Session
from ovos_workshop.skills.ovos import OVOSSkill

from ovoscope import (get_m2v_minicroft, is_pipeline_available,
                      m2v_model_labels, assert_m2v_label_split,
                      CaptureSession,
                      M2V_PIPELINE, M2V_DUAL_PIPELINE, M2V_MULTILINGUAL_MODEL)

# The canonical author-qualified id the installed skill registers under, and the
# id the multilingual model's labels are trained on.
SKILL_ID = "ovos-skill-personal.openvoiceos"

LIVE = os.environ.get("OVOSCOPE_LIVE") == "1"

_MC = None
_PIPE = None
_XDG = None
_ORIG_XDG = None


def setUpModule():
    global _MC, _PIPE, _XDG, _ORIG_XDG
    if not LIVE or not is_pipeline_available(M2V_PIPELINE):
        return

    _ORIG_XDG = os.environ.get("XDG_DATA_HOME")
    _XDG = tempfile.mkdtemp(prefix="ovoscope-m2v-personal-xdg-")
    os.environ["XDG_DATA_HOME"] = _XDG

    # Normal skill-plugin install: the skill registers its intents under its
    # canonical author-qualified id, which the model's labels are trained on.
    _MC = get_m2v_minicroft(
        skill_ids=[SKILL_ID],
        lang="en-US",
        secondary_langs=["es-ES"],
    )
    _PIPE = _MC.intents.pipeline_plugins["ovos-m2v-pipeline"]
    # The model loads lazily on first match; force it now so the first real
    # utterance is not silently skipped while the model is still warming up.
    _PIPE._ensure_model(background_ok=False)


def tearDownModule():
    global _MC, _XDG, _ORIG_XDG
    if _MC is not None:
        _MC.stop()
        _MC = None
    if _ORIG_XDG is None:
        os.environ.pop("XDG_DATA_HOME", None)
    else:
        os.environ["XDG_DATA_HOME"] = _ORIG_XDG
    if _XDG is not None:
        shutil.rmtree(_XDG, ignore_errors=True)
        _XDG = None


@unittest.skipUnless(LIVE, "live m2v model test; set OVOSCOPE_LIVE=1 to enable")
class TestM2VMultilingualGoldenEffect(unittest.TestCase):
    """Effect assertions: utterance in -> correct intent -> real words out."""

    def _run(self, utterance: str, lang: str, timeout: float = 15.0):
        """Fire one utterance and return (dialog_name, rendered_text, failed).

        dialog_name/rendered_text come from the captured ``speak``;
        ``failed`` is True if a ``complete_intent_failure`` arrived instead.
        """
        speaks: list = []
        failures: list = []

        def _on_speak(msg):
            speaks.append(msg)

        def _on_fail(msg):
            failures.append(msg)

        self._MC = _MC
        _MC.bus.on("speak", _on_speak)
        _MC.bus.on("complete_intent_failure", _on_fail)
        sess = Session(session_id=f"m2v-golden-{hash(utterance)}",
                       pipeline=M2V_PIPELINE)
        sess.lang = lang
        try:
            _MC.bus.emit(Message(
                "recognizer_loop:utterance",
                data={"utterances": [utterance], "lang": lang},
                context={"session": sess.serialize(), "lang": lang},
            ))
            import time as _t
            deadline = _t.time() + timeout
            while _t.time() < deadline and not speaks and not failures:
                _t.sleep(0.05)
        finally:
            _MC.bus.remove("speak", _on_speak)
            _MC.bus.remove("complete_intent_failure", _on_fail)

        if not speaks:
            return None, None, bool(failures)
        data = speaks[0].data
        meta = data.get("meta", {}) or {}
        return meta.get("dialog"), (data.get("utterance") or ""), False

    def _assert_effect(self, utterance, lang, expected_dialog, expected_substrings):
        dialog, text, failed = self._run(utterance, lang)
        self.assertFalse(
            failed,
            f"{utterance!r} ({lang}) did not route: complete_intent_failure")
        self.assertIsNotNone(
            text, f"{utterance!r} ({lang}) produced no spoken output")
        low = text.lower()
        # Routing: the intent-specific dialog was rendered.
        self.assertEqual(
            dialog, expected_dialog,
            f"{utterance!r} ({lang}) routed to dialog {dialog!r}, "
            f"expected {expected_dialog!r} (rendered: {text!r})")
        # Effect: real words / slot values, never the raw dialog file name.
        self.assertNotIn(
            expected_dialog, low,
            f"{utterance!r} ({lang}) spoke the dialog NAME, not rendered text: {text!r}")
        for sub in expected_substrings:
            self.assertIn(
                sub.lower(), low,
                f"{utterance!r} ({lang}) rendered {text!r}; missing {sub!r}")

    # The who_am_i dialog has two variants and its {name} slot depends on the
    # configured wake word, so assert on the variant-invariant words.
    def test_who_are_you_en_effect(self):
        dialog, text, failed = self._run("who are you", "en-US")
        self.assertFalse(failed, "who are you did not route")
        self.assertEqual(dialog, "who_am_i")
        low = text.lower()
        self.assertTrue(
            "open-source" in low or "intelligent personal assistant" in low,
            f"unexpected who_am_i rendering: {text!r}")
        self.assertNotIn("who_am_i", low)

    def test_what_are_you_en(self):
        dialog, text, failed = self._run("what are you", "en-US")
        self.assertFalse(failed, "what are you did not route")
        self.assertEqual(dialog, "what_am_i")
        low = text.lower()
        self.assertTrue(
            "artificial intelligence" in low or "software" in low,
            f"unexpected what_am_i rendering: {text!r}")
        self.assertNotIn("what_am_i", low)

    def test_who_made_you_en(self):
        # who_made_me renders the {creator} slot (default OpenVoiceOS).
        self._assert_effect(
            "who made you", "en-US", "who_made_me",
            ["openvoiceos", "community"])

    def test_when_were_you_born_en(self):
        # when_was_i_born renders the {year} slot (default 2015).
        self._assert_effect(
            "when were you born", "en-US", "when_was_i_born", ["2015"])

    def test_where_were_you_born_en(self):
        # where_was_i_born renders the {location} slot (default Lawrence Kansas).
        self._assert_effect(
            "where were you born", "en-US", "where_was_i_born", ["lawrence kansas"])

    def test_who_made_you_es(self):
        # Non-en-US: Spanish routes to the same intent; the es-ES who_made_me
        # dialog renders "...comunidad y el equipo de OpenVoiceOS".
        self._assert_effect(
            "quién te creó", "es-ES", "who_made_me", ["openvoiceos", "comunidad"])


@unittest.skipUnless(LIVE, "live m2v model test; set OVOSCOPE_LIVE=1 to enable")
class TestM2VRegisteredLabelRouting(unittest.TestCase):
    """The model's labels carry the canonical skill id and route it."""

    def test_registered_skill_id_labels_route(self):
        classes = {str(c) for c in _PIPE.model.classes_}
        # The model is trained on author-qualified labels...
        self.assertIn(f"{SKILL_ID}:who_are_you", classes)
        # ...and carries no bare-package-name labels for this skill.
        self.assertNotIn("ovos-skill-personal:who_are_you", classes)
        # The skill registers under its canonical id, so its runtime labels
        # intersect the model's trained classes -> it can route.
        registered = set(_PIPE.intents)
        self.assertIn(f"{SKILL_ID}:who_are_you", registered)
        self.assertTrue(
            registered & classes,
            "registered intent labels do not intersect the model's classes; "
            "the skill would route nothing through this model")


# A label the multilingual checkpoint was never trained on, used to prove
# prototype mode serves labels the classifier cannot.
FROBNICATE_SKILL_ID = "ovoscope-unittest-proto.test"
FROBNICATE_LABEL = f"{FROBNICATE_SKILL_ID}:frobnicate"
FROBNICATE_SAMPLES = [
    "frobnicate the widget",
    "please frobnicate my widget",
    "widget frobnication now",
]


class FrobnicateSkill(OVOSSkill):
    """Registers one Padatious-style intent via inline samples, exactly as
    ovos_workshop.intents.emit_legacy_register_template emits it, for a
    label that does not exist in the m2v checkpoint's trained class list."""

    def initialize(self):
        self.bus.emit(Message(
            "padatious:register_intent",
            {"file_name": "", "samples": FROBNICATE_SAMPLES,
             "name": FROBNICATE_LABEL, "lang": "en-us"},
            context={"skill_id": self.skill_id}))


@unittest.skipUnless(LIVE, "live m2v model test; set OVOSCOPE_LIVE=1 to enable")
class TestM2VDualModeBoot(unittest.TestCase):
    """``get_m2v_minicroft(prototype=True)`` boots classifier + prototype."""

    @classmethod
    def setUpClass(cls):
        if not is_pipeline_available(M2V_DUAL_PIPELINE):
            raise unittest.SkipTest(
                "ovos-m2v-prototype-pipeline not installed")

    def test_m2v_model_labels_are_canonical(self):
        labels = m2v_model_labels(M2V_MULTILINGUAL_MODEL)
        self.assertTrue(labels, "model carries no trained labels")
        self.assertTrue(all(":" in label for label in labels))
        self.assertTrue(
            any(".openvoiceos:" in label for label in labels),
            "no canonical <repo>.<org>:<intent> label found")

    def _boot_dual(self, **kwargs):
        xdg = tempfile.mkdtemp(prefix="ovoscope-m2v-dual-xdg-")
        orig_xdg = os.environ.get("XDG_DATA_HOME")
        os.environ["XDG_DATA_HOME"] = xdg

        def _cleanup():
            if orig_xdg is None:
                os.environ.pop("XDG_DATA_HOME", None)
            else:
                os.environ["XDG_DATA_HOME"] = orig_xdg
            shutil.rmtree(xdg, ignore_errors=True)

        self.addCleanup(_cleanup)
        kwargs.setdefault("skill_ids", [])
        kwargs.setdefault("extra_skills", {FROBNICATE_SKILL_ID: FrobnicateSkill})
        kwargs.setdefault("lang", "en-US")
        mc = get_m2v_minicroft(**kwargs)
        self.addCleanup(mc.stop)
        # The model loads lazily on first match; force it now so the first
        # utterance in a test is not silently skipped while it warms up.
        for pipe_id in ("ovos-m2v-pipeline", "ovos-m2v-prototype-pipeline"):
            plugin = mc.intents.pipeline_plugins.get(pipe_id)
            if plugin is not None:
                plugin._ensure_model(background_ok=False)
        return mc

    def test_dual_pipeline_boots_both_stages(self):
        model_labels = set(m2v_model_labels(M2V_MULTILINGUAL_MODEL))
        self.assertNotIn(FROBNICATE_LABEL, model_labels)

        mc = self._boot_dual()
        self.assertIn("ovos-m2v-pipeline", mc.intents.pipeline_plugins)
        self.assertIn("ovos-m2v-prototype-pipeline", mc.intents.pipeline_plugins)
        self.assertEqual(mc.pipeline, M2V_DUAL_PIPELINE)
        proto = mc.intents.pipeline_plugins["ovos-m2v-prototype-pipeline"]
        self.assertEqual(set(proto.ignore_labels), model_labels)

    def test_prototype_serves_labels_the_model_lacks(self):
        mc = self._boot_dual()
        cap = CaptureSession(mc)
        sess = Session(session_id="m2v-dual-frobnicate", pipeline=M2V_DUAL_PIPELINE)
        sess.lang = "en-US"
        msg = Message(
            "recognizer_loop:utterance",
            data={"utterances": ["frobnicate the widget"], "lang": "en-US"},
            context={"session": sess.serialize(), "lang": "en-US"})
        cap.capture(msg, timeout=20)
        matched = [m for m in cap.responses if m.msg_type == FROBNICATE_LABEL]
        self.assertTrue(
            matched,
            f"{FROBNICATE_LABEL!r} never matched; captured types: "
            f"{[m.msg_type for m in cap.responses]}")
        proto = mc.intents.pipeline_plugins["ovos-m2v-prototype-pipeline"]
        self.assertIn(FROBNICATE_LABEL, proto.intents)
        classifier = mc.intents.pipeline_plugins["ovos-m2v-pipeline"]
        self.assertNotIn(
            FROBNICATE_LABEL, classifier.model.classes_,
            "the classifier cannot have this label; it is not in the model")

    def test_classifier_still_serves_model_labels_under_dual_mode(self):
        mc = self._boot_dual(skill_ids=[SKILL_ID])
        cap = CaptureSession(mc)
        sess = Session(session_id="m2v-dual-who-are-you", pipeline=M2V_DUAL_PIPELINE)
        sess.lang = "en-US"
        speaks = []
        mc.bus.on("speak", speaks.append)
        try:
            msg = Message(
                "recognizer_loop:utterance",
                data={"utterances": ["who are you"], "lang": "en-US"},
                context={"session": sess.serialize(), "lang": "en-US"})
            cap.capture(msg, timeout=20)
        finally:
            mc.bus.remove("speak", speaks.append)
        self.assertTrue(speaks, "who are you produced no spoken output")
        text = (speaks[0].data.get("utterance") or "").lower()
        self.assertTrue(
            "open-source" in text or "intelligent personal assistant" in text,
            f"unexpected who_am_i rendering under dual mode: {text!r}")


@unittest.skipUnless(LIVE, "live m2v model test; set OVOSCOPE_LIVE=1 to enable")
class TestM2VLabelSplitAssertion(unittest.TestCase):
    """``assert_m2v_label_split`` fails loudly on a broken classifier /
    prototype label partition."""

    @classmethod
    def setUpClass(cls):
        if not is_pipeline_available(M2V_DUAL_PIPELINE):
            raise unittest.SkipTest(
                "ovos-m2v-prototype-pipeline not installed")

    def _boot(self, **kwargs):
        xdg = tempfile.mkdtemp(prefix="ovoscope-m2v-split-xdg-")
        orig_xdg = os.environ.get("XDG_DATA_HOME")
        os.environ["XDG_DATA_HOME"] = xdg

        def _cleanup():
            if orig_xdg is None:
                os.environ.pop("XDG_DATA_HOME", None)
            else:
                os.environ["XDG_DATA_HOME"] = orig_xdg
            shutil.rmtree(xdg, ignore_errors=True)

        self.addCleanup(_cleanup)
        mc = get_m2v_minicroft(skill_ids=[SKILL_ID], lang="en-US", **kwargs)
        self.addCleanup(mc.stop)
        return mc

    def test_no_registered_labels_raises(self):
        # A boot where no skill loaded registers nothing: both engine sets
        # are empty, so disjointness and coverage hold vacuously. That must
        # not read as a sound split — it is the case where the suite would
        # measure nothing.
        xdg = tempfile.mkdtemp(prefix="ovoscope-m2v-split-empty-xdg-")
        orig_xdg = os.environ.get("XDG_DATA_HOME")
        os.environ["XDG_DATA_HOME"] = xdg
        self.addCleanup(shutil.rmtree, xdg, True)
        self.addCleanup(lambda: os.environ.update({"XDG_DATA_HOME": orig_xdg})
                        if orig_xdg is not None
                        else os.environ.pop("XDG_DATA_HOME", None))
        with self.assertRaises(RuntimeError) as ctx:
            get_m2v_minicroft(skill_ids=[], lang="en-US")
        self.assertIn("no intent labels were registered", str(ctx.exception))

    def test_duplicate_label_in_both_engines_raises(self):
        # who_are_you is one of the model's trained labels. Omitting it from
        # the prototype deny-list lets prototype mode ALSO register it (the
        # skill's real .intent file registration reaches both plugins), so
        # the label ends up served by both engines.
        model_labels = set(m2v_model_labels(M2V_MULTILINGUAL_MODEL))
        who_are_you = f"{SKILL_ID}:who_are_you"
        self.assertIn(who_are_you, model_labels)
        broken_ignore = sorted(model_labels - {who_are_you})
        with self.assertRaises(RuntimeError) as ctx:
            self._boot(prototype_ignore_intents=broken_ignore)
        self.assertIn("in both", str(ctx.exception))
        self.assertIn(who_are_you, str(ctx.exception))

    def test_orphaned_label_in_neither_engine_raises(self):
        # Deny-list the same label on BOTH engines: the classifier via
        # ignore_intents, the prototype stage via prototype_ignore_intents.
        # Neither engine will ever serve it.
        who_are_you = f"{SKILL_ID}:who_are_you"
        model_labels = set(m2v_model_labels(M2V_MULTILINGUAL_MODEL))
        proto_ignore = sorted(model_labels)  # unchanged (correct) deny-list
        with self.assertRaises(RuntimeError) as ctx:
            self._boot(ignore_intents=[who_are_you],
                       prototype_ignore_intents=proto_ignore)
        self.assertIn("in neither", str(ctx.exception))
        self.assertIn(who_are_you, str(ctx.exception))
