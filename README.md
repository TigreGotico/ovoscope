[![Ask DeepWiki](https://deepwiki.com/badge.svg)](https://deepwiki.com/TigreGotico/ovoscope)
[![PyPI](https://img.shields.io/pypi/v/ovoscope)](https://pypi.org/project/ovoscope/)
[![License](https://img.shields.io/badge/license-Apache%202.0-blue.svg)](LICENSE)
[![Python](https://img.shields.io/badge/python-3.10%2B-blue)](https://pypi.org/project/ovoscope/)
# OvoScope

**End-to-end testing for [OVOS](https://openvoiceos.org) skills.**

OvoScope runs a full OVOS Core pipeline in-process with a `FakeBus`. It needs no server, no
audio stack, and no network. Load real skill plugins, send a test utterance, and check every
bus message that comes back: type, data, routing context, session state, and message order.

![image](https://github.com/user-attachments/assets/10a10ff5-64b7-42fd-86bd-cb6a5db769dd)

---
## Features
| | |
|---|---|
| **Full pipeline** | Runs real intent pipeline plugins (Adapt, Padatious, Fallback, Converse, Common Query) |
| **Isolated** | Config isolation strips user preferences, and the deterministic `DEFAULT_TEST_PIPELINE` excludes AI, persona, and OCP stages |
| **Ordered assertions** | Checks message type, data keys, routing context, and session state in order |
| **Recording mode** | Captures a live message sequence and saves it as a JSON fixture. No manual construction needed |
| **Multi-turn** | Pass a list of utterances to test full conversational flows |
| **pytest fixture** | The `minicroft` class-scoped fixture is auto-discovered through the `pytest11` entry point |
| **Inject skills** | Use `extra_skills={id: SkillClass}` to load inline test skills without a PyPI entry point |
| **Inject messages** | Use `MiniCroft.inject_message()` to trigger non-utterance handlers (GUI events, timers, API calls) |
| **Typed models** | The optional `ovoscope[pydantic]` bridge adds schema-validated messages through `ovos-pydantic-models` |
---
## Installation
```bash
pip install ovoscope
```
To add typed message model support:
```bash
pip install ovoscope[pydantic]
```
---
## Quick Start
```python
import unittest
from ovos_bus_client.message import Message
from ovos_bus_client.session import Session
from ovoscope import End2EndTest
SKILL_ID = "ovos-skill-hello-world.openvoiceos"
session = Session("test-session")
utterance = Message(
    "recognizer_loop:utterance",
    {"utterances": ["hello world"], "lang": "en-US"},
    {"session": session.serialize(), "source": "A", "destination": "B"},
)
class TestHelloWorld(unittest.TestCase):
    def test_intent_match(self):
        End2EndTest(
            skill_ids=[SKILL_ID],
            source_message=utterance,
            expected_messages=[
                utterance,
                Message(f"{SKILL_ID}.activate", context={"skill_id": SKILL_ID}),
                Message(f"{SKILL_ID}:HelloWorldIntent",
                        data={"utterance": "hello world"}, context={"skill_id": SKILL_ID}),
                Message("mycroft.skill.handler.start", context={"skill_id": SKILL_ID}),
                Message("speak", data={"lang": "en-US"}, context={"skill_id": SKILL_ID}),
                Message("mycroft.skill.handler.complete", context={"skill_id": SKILL_ID}),
                Message("ovos.utterance.handled", context={"skill_id": SKILL_ID}),
            ],
        ).execute(timeout=10)
```
OvoScope checks only the keys you list in `expected.data` and `expected.context`. It ignores
extra keys in the received message.
---
## Recording Mode
If you do not know the exact message sequence yet, record it from a live run:
```python
from ovoscope import End2EndTest
test = End2EndTest.from_message(
    message=utterance,
    skill_ids=[SKILL_ID],
    timeout=20,
)
test.save("tests/fixtures/hello_world.json")  # anonymizes location data by default
```
Replay the fixture in CI:
```python
End2EndTest.from_path("tests/fixtures/hello_world.json").execute(timeout=10)
```
---
## pytest Fixture
OvoScope auto-registers the `minicroft` class-scoped fixture on install. You do not need
`setUp`/`tearDown` boilerplate:
```python
class TestMySkill:
    skill_ids = ["my-skill.author"]
    def test_something(self, minicroft):
        End2EndTest(
            minicroft=minicroft,
            skill_ids=self.skill_ids,
            source_message=utterance,
            expected_messages=[...],
        ).execute(timeout=10)
```
---
## Pipeline Control
OvoScope exposes composable pipeline stage lists so tests stay deterministic regardless of
which AI plugins are installed on the host:
```python
from ovoscope import ADAPT_PIPELINE, PADATIOUS_PIPELINE, FALLBACK_PIPELINE, PERSONA_PIPELINE
# Adapt only: fastest
mc = get_minicroft([SKILL_ID], default_pipeline=ADAPT_PIPELINE)
# Full intent chain
mc = get_minicroft([SKILL_ID],
                   default_pipeline=ADAPT_PIPELINE + PADATIOUS_PIPELINE + FALLBACK_PIPELINE)
# Opt in to persona for AI testing
mc = get_minicroft([SKILL_ID], default_pipeline=DEFAULT_TEST_PIPELINE + PERSONA_PIPELINE)
```
`DEFAULT_TEST_PIPELINE` is the default when `isolate_config=True`. It includes all standard
built-in stages and leaves out persona, Ollama, OCP, and m2v plugins.
---
## Documentation
| Document | |
|---|---|
| [docs/usage-guide.md](docs/usage-guide.md) | **Start here**: 8 test patterns with full worked examples |
| [docs/ci-integration.md](docs/ci-integration.md) | Wiring OvoScope into GitHub Actions |
| [docs/minicroft.md](docs/minicroft.md) | `MiniCroft` and `get_minicroft()` reference |
| [docs/capture-session.md](docs/capture-session.md) | `CaptureSession` internals |
| [docs/end2end-test.md](docs/end2end-test.md) | `End2EndTest` full parameter reference |
| [docs/e2e-pipeline-harness.md](docs/e2e-pipeline-harness.md) | `E2EPipelineHarness` — testing a single pipeline plugin against raw bus messages |
| [docs/intent-cases.md](docs/intent-cases.md) | File-based intent test cases (`.intent.test`) via `register_intent_case_tests` |
| [docs/pydantic-integration.md](docs/pydantic-integration.md) | Typed message models with `ovos-pydantic-models` |
| [docs/cli.md](docs/cli.md) | `ovoscope` CLI — record/run/diff/validate/coverage/bus-coverage, plus `ovoscope-setup` |
| [FAQ.md](FAQ.md) | Common questions and gotchas |
---

---

## Related Projects

OvoScope is part of the [OpenVoiceOS](https://github.com/OpenVoiceOS) tooling suite:

- [ovos-core](https://github.com/OpenVoiceOS/ovos-core): the OVOS assistant core that OvoScope tests skills against.
- [ovos-workshop](https://github.com/OpenVoiceOS/ovos-workshop): the skill base classes that OvoScope loads and drives.
- [ovos-bus-client](https://github.com/OpenVoiceOS/ovos-bus-client): the message bus client behind `FakeBus` and `Message`.
- [ovos-test-harness](https://github.com/OpenVoiceOS/ovos-test-harness): a companion test harness for OVOS components.

## Credits

Developed by [TigreGótico](https://tigregotico.pt) for
[OpenVoiceOS](https://openvoiceos.org).

[![NGI0 Commons Fund](./ngi.png)](https://nlnet.nl/project/OpenVoiceOS)

This project was funded through the [NGI0 Commons Fund](https://nlnet.nl/commonsfund),
a fund established by [NLnet](https://nlnet.nl) with financial support from the
European Commission's [Next Generation Internet](https://ngi.eu) programme, under
the aegis of [DG Communications Networks, Content and Technology](https://commission.europa.eu/about-european-commission/departments-and-executive-agencies/communications-networks-content-and-technology_en)
under grant agreement No [101135429](https://cordis.europa.eu/project/id/101135429).

---

## License

[Apache 2.0](LICENSE)

---

## Contributing

PRs are welcome. See [CONTRIBUTING.md](CONTRIBUTING.md) for guidelines.

---

## AI Disclosure

Parts of this project — including code, tests, and documentation — are developed with the
assistance of AI coding agents, under human review before merge. Commit messages and pull
request descriptions in the [git history](https://github.com/TigreGotico/ovoscope/commits/dev)
and [CHANGELOG.md](CHANGELOG.md) note when a change originated from an AI-assisted session, so
contributors and users can see where AI assistance has been applied.
