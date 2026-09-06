# Pre-release quirks

Behavior changes since the last stable release, newest first. This file is
reset at each stable release.

## next alpha

- `get_m2v_minicroft()` now boots the model2vec classifier and model2vec
  prototype mode side by side by default (`prototype=True`), via the new
  `M2V_DUAL_PIPELINE`, so a skill's labels route whether or not they are in
  the trained checkpoint; pass `prototype=False` for the classifier-only
  behavior. `assert_m2v_label_split()` is a new public check callable on any
  booted dual-mode `MiniCroft`. See
  [docs/minicroft.md](minicroft.md#m2v-boot-mode).
- `get_minicroft()` skips the `mycroft.skills.trained` wait when no pipeline
  plugin on the bus subscribes to `mycroft.skills.train` — the m2v and
  adapt pipelines register intents synchronously and never send that
  reply, so a boot using only those (`default_pipeline=M2V_PIPELINE`, or
  adapt-only) used to wait out the full `OVOSCOPE_TRAINED_TIMEOUT` and
  raise `RuntimeError` no matter how large the timeout. It now returns at
  `READY` in that case. See [OpenVoiceOS/ovoscope#179](https://github.com/OpenVoiceOS/ovoscope/issues/179)
  and [docs/minicroft.md](minicroft.md#factory-get_minicroft).
- `get_minicroft()` now waits for `mycroft.skills.trained` to go quiet after
  `READY`, but only when a loaded skill actually registered an intent
  (mirrors the pipeline plugin's own `needs_compile` gate). If training
  never completes within `OVOSCOPE_TRAINED_TIMEOUT` seconds it raises
  `RuntimeError` naming only the skill(s) that registered an intent and
  never got a trained reply, not every skill in the load — instead of
  returning a croft whose intents aren't actually matchable yet. Opt out
  with `wait_for_trained=False`. See
  [docs/minicroft.md](minicroft.md#factory-get_minicroft).
- `DEFAULT_IGNORED` (and therefore every exact sequence comparison) now also
  filters `mycroft.skills.trained` (`TRAINING_NOISE`), MiniCroft's own
  training orchestration noise, before comparing — never by subsequence
  matching, so a genuinely missing or duplicated message still fails. See
  [docs/capture-session.md](capture-session.md#default-ignored-messages).
- `ovoscope`'s own `ovos-core` dependency now pulls the `[lgpl,plugins]`
  extras (same pair the installer itself uses). Bare `ovos-core` ships no
  pipeline matchers at all; `[plugins]` alone covers Adapt and Padacioso but
  not Padatious, which is LGPL-licensed and lives in `[lgpl]` — a
  `[plugins]`-only pin still silently drops every Padatious-registered
  intent to no match. Test environments that installed `ovos-core`
  explicitly (bypassing ovoscope's own dependency) still need both extras
  (or the specific pipeline plugins under test) themselves. See
  [docs/minicroft.md](minicroft.md#factory-get_minicroft).
- The `pytest_pycollect_makemodule` hook no longer lets a module-level
  `pytest.skip()`/`pytest.importorskip()` abort the whole collection
  session. `pytest.skip.Exception` derives from `BaseException`, not
  `Exception`, so it used to slip past the hook's guard, escape uncaught,
  and make pytest report zero collected items for the entire run. It is
  now swallowed the same way as any other import-time failure, so only the
  module that asked to be skipped is affected.
- `CaptureSession.capture()` now ends on the pipeline's own terminal signal
  (`ovos.utterance.handled`) in addition to whatever `eof_msgs` was given,
  so an unmatched or misrouted utterance no longer pays the full `timeout`
  when a caller has narrowed `eof_msgs` down to a topic only a matched
  utterance reaches. Opt out with `terminal_signals=False`; the merge is
  skipped automatically when `eof_count > 1`. See
  [docs/capture-session.md](capture-session.md#terminal-signals).
- `intent_cases` now asserts the OVOS-INTENT-4 canonical (suffixless) intent
  id: matcher plugins (ovos-padatious 2.0+) fold the legacy `<file>.intent`
  suffix off at registration, so the expected dispatch topic is
  `<skill_id>:<IntentName>`, not `<skill_id>:<IntentName>.intent`. Case files
  keep their `<Intent>.intent.test` naming; suffixed entries in
  `known_intents` and `handlers` are still accepted and folded the same way,
  so existing suites keep working unchanged.
