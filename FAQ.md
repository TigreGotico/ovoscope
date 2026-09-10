# FAQ

Common questions and gotchas when using OvoScope.

## Does OvoScope need a real OVOS install running?
No. `MiniCroft` runs the skill manager and intent pipeline in-process on a
`FakeBus` — there is no WebSocket server, no PulseAudio/audio stack, and no
network access required for `record`/`run`/`diff`/`validate`. See
[docs/index.md](docs/index.md) for what the in-process harness does and does
not cover, and [docs/phal.md](docs/phal.md) / [docs/audio-testing.md](docs/audio-testing.md)
for the separate PHAL and audio harnesses.

## Why did my test fail with extra/missing messages I didn't expect?
Only the keys you specify in `expected.data` and `expected.context` are
checked — extra keys in the received message are ignored. If a message you
didn't list is showing up as unexpected, check `ignore_messages` (see
[docs/end2end-test.md](docs/end2end-test.md)) — some message types (e.g.
`speak`, `ovos.skills.settings_changed`) are noisy or non-deterministic and
are commonly filtered out.

## My fixture recording is flaky / times out.
Increase `--timeout` on `ovoscope record`, or the `timeout` kwarg on
`End2EndTest.from_message`. If the skill under test depends on plugins that
take time to warm up (e.g. `ovos-m2v-pipeline` syncing its label index),
see `m2v_warmup` in [docs/intent-cases.md](docs/intent-cases.md) for how
`ovoscope.intent_cases` handles that deterministically.

## How do I test a skill that isn't installed as a plugin yet?
Pass it via `extra_skills={"my-skill.test": MySkillClass}` to `MiniCroft` /
`get_minicroft()` instead of `skill_ids`. See
[docs/minicroft.md](docs/minicroft.md).

## How do I test multiple languages?
Pass `secondary_langs=[...]` to `get_minicroft()` so Adapt/Padatious
register vocab for each locale. See "Multilingual Testing" in
[docs/minicroft.md](docs/minicroft.md).

## Where do I report a bug or ask something not covered here?
Open an issue on [GitHub](https://github.com/TigreGotico/ovoscope/issues).
