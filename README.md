# OvoScope

**OvoScope** is an end-to-end testing framework for [OVOS](https://openvoiceos.org) skills. 

It contains the full core runtime environment using a lightweight in-process `ovos-core`, allowing skill developers to test the full skill message flow, from utterance to intent handling to final bus responses — without launching a full assistant stack.

> Like a microscope for your OVOS skills.

---

## Features

- Simulates OVOS Core messagebus interactions
- Sends test `Message` objects and captures responses
- Verifies message types, data, routing, session handling, and language
- Automatically flips message direction when configured
- Designed to integrate cleanly into `unittest` or `pytest` workflows

---

## Installation

```bash
pip install ovoscope
````

---

## Usage Example

Testing scenario of complete intent failure (no skills installed)

```python
from ovoscope import End2EndTest
from ovos_bus_client.message import Message
from ovos_bus_client.session import Session


session = Session("123")  # change lang, pipeline, etc. as needed
message = Message("recognizer_loop:utterance",
                  {"utterances": ["hello world"]},
                  {"session": session.serialize(), "source": "A", "destination": "B"})

test = End2EndTest(
    skill_ids=[],
    source_message=message,
    expected_messages=[
        message,
        Message("mycroft.audio.play_sound", {"uri": "snd/error.mp3"}),
        Message("complete_intent_failure", {}),
        Message("ovos.utterance.handled", {}),
    ]
)

test.execute()
```

---

## Why OvoScope?

* Lightweight: No need to launch a full messagebus or audio stack
* Isolated: Use `FakeBus` and `MiniCroft` for fast, reliable test environments
* Flexible: Works with any skill that conforms to OVOS skill loading

---

## License

[Apache 2.0](LICENSE)

---

## Contributing

PRs are welcome! See [CONTRIBUTING.md](CONTRIBUTING.md) for guidelines.
