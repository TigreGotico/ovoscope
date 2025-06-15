import dataclasses
import json
import threading
from copy import deepcopy
from time import sleep
from typing import Union, List, Dict, Any, Optional

from ovos_bus_client.message import Message
from ovos_bus_client.session import SessionManager, Session
from ovos_bus_client.util.scheduler import EventScheduler
from ovos_core.intent_services import IntentService
from ovos_core.skill_manager import SkillManager
from ovos_plugin_manager.skills import find_skill_plugins
from ovos_utils.fakebus import FakeBus
from ovos_utils.log import LOG
from ovos_utils.process_utils import ProcessState

SerializedMessage = Dict[str, Union[str, Dict[str, Any]]]
SerializedTest = Dict[str, Union[str, bool, List[str], SerializedMessage]]

DEFAULT_IGNORED = ["ovos.skills.settings_changed"]
GUI_IGNORED = ["gui.clear.namespace",
               "gui.value.set",
               "mycroft.gui.screen.close",
               "gui.page.show"]
DEFAULT_EOF = ["ovos.utterance.handled", "skill.converse.response"]
DEFAULT_FLIP_POINTS = ["recognizer_loop:utterance"]
DEFAULT_KEEP_SRC = ["ovos.skills.fallback.ping"]


class MiniCroft(SkillManager):
    def __init__(self, skill_ids, *args, **kwargs):
        self.boot_messages: List[Message] = []
        bus = FakeBus()
        bus.on("message", self.handle_boot_message)
        self.skill_ids = skill_ids
        self.intent_service = IntentService(bus)
        self.scheduler = EventScheduler(bus, schedule_file="/tmp/schetest.json")
        super().__init__(bus, *args, **kwargs)

    def handle_boot_message(self, message: str):
        self.boot_messages.append(Message.deserialize(message))

    def load_metadata_transformers(self, cfg):
        self.intent_service.metadata_plugins.config = cfg
        self.intent_service.metadata_plugins.load_plugins()

    def load_plugin_skills(self):
        LOG.info("loading skill plugins")
        plugins = find_skill_plugins()
        for skill_id, plug in plugins.items():
            LOG.debug(f"Found skill: {skill_id}")
            if skill_id not in self.skill_ids:
                continue
            if skill_id not in self.plugin_skills:
                self._load_plugin_skill(skill_id, plug)
                LOG.info(f"Loaded skill: {skill_id}")

        self.bus.emit(Message("mycroft.skills.train"))  # tell any pipeline plugins to train loaded intents

    def run(self):
        """Load skills and mark core as ready to start tests"""
        self.status.set_alive()
        self.load_plugin_skills()
        LOG.info("Skills all loaded!")
        self.status.set_ready()
        self.bus.remove("message", self.handle_boot_message)

    def stop(self):
        super().stop()
        self.scheduler.shutdown()
        self.bus.close()


def get_minicroft(skill_ids: Union[List[str], str]):
    if isinstance(skill_ids, str):
        skill_ids = [skill_ids]
    assert isinstance(skill_ids, list)
    croft1 = MiniCroft(skill_ids)
    croft1.start()
    while croft1.status.state != ProcessState.READY:
        sleep(0.2)
    return croft1


@dataclasses.dataclass()
class CaptureSession:
    minicroft: MiniCroft
    responses: List[Message] = dataclasses.field(default_factory=list)
    eof_msgs: List[str] = dataclasses.field(default_factory=lambda: DEFAULT_EOF)
    ignore_messages: List[str] = dataclasses.field(default_factory=lambda: DEFAULT_IGNORED)
    done: threading.Event = dataclasses.field(default_factory=lambda: threading.Event())

    def handle_message(self, msg: str):
        if self.done.is_set():
            return
        msg = Message.deserialize(msg)
        if msg.msg_type not in self.ignore_messages:
            self.responses.append(msg)

    def handle_end_of_test(self, msg: Message):
        self.done.set()

    def __post_init__(self):
        self.minicroft.bus.on("message", self.handle_message)
        for m in self.eof_msgs:
            self.minicroft.bus.on(m, self.handle_end_of_test)

    def capture(self, source_message: Message, timeout=20):
        test_message = deepcopy(source_message)  # ensure object not mutated by ovos-core
        self.done.clear()
        self.minicroft.bus.emit(test_message)
        self.done.wait(timeout)

    def finish(self) -> List[Message]:
        self.done.set()
        self.minicroft.bus.remove("message", self.handle_message)
        for m in self.eof_msgs:
            self.minicroft.bus.remove(m, self.handle_end_of_test)
        return self.responses

    def __del__(self):
        self.finish()


@dataclasses.dataclass()
class End2EndTest:
    skill_ids: List[str]  # skill_ids to load during the test
    source_message: Union[Message, List[Message]]  # to be emitted, sequentially if a list
    expected_messages: List[Message]  # tests are performed against message list
    expected_boot_sequence: List[Message] = dataclasses.field(default_factory=list)  # check before any tests are run
    ignore_messages: List[str] = dataclasses.field(default_factory=lambda: DEFAULT_IGNORED)
    ignore_gui: bool = True
    inject_active: List[str] = dataclasses.field(default_factory=list)

    # if received, end message capture
    eof_msgs: List[str] = dataclasses.field(default_factory=lambda: DEFAULT_EOF)

    # messages after which source and destination flip in the message.context
    flip_points: List[str] = dataclasses.field(default_factory=lambda: DEFAULT_FLIP_POINTS)
    keep_original_src: List[str] = dataclasses.field(default_factory=lambda: DEFAULT_KEEP_SRC)

    activation_points: Dict[str, str] = dataclasses.field(default_factory=dict)
    deactivation_points: Dict[str, str] = dataclasses.field(default_factory=dict)

    minicroft: Optional[MiniCroft] = None
    managed: bool = False

    # test assertions to run
    test_message_number: bool = True
    test_boot_sequence: bool = True
    test_msg_type: bool = True
    test_msg_data: bool = True
    test_msg_context: bool = True
    test_active_skills: bool = True
    test_routing: bool = True

    def __post_init__(self):
        # standardize to be a list
        if isinstance(self.source_message, Message):
            self.source_message = [self.source_message]
        if self.ignore_gui:
            self.ignore_messages += GUI_IGNORED

    def execute(self, timeout=30):
        if self.minicroft is None:
            self.minicroft = get_minicroft(self.skill_ids)
            self.managed = True

        if self.test_boot_sequence and self.expected_boot_sequence:
            for expected, received in zip(self.expected_boot_sequence, self.minicroft.boot_messages):
                assert expected.msg_type == received.msg_type, f"expected boot message_type '{expected.msg_type}' | got '{received.msg_type}'"
                for k, v in expected.data.items():
                    assert received.data[k] == v
                for k, v in expected.context.items():
                    assert received.context[k] == v

        sess = SessionManager.get(self.source_message[0])
        for s in self.inject_active:
            print(f"activating skill pre-test: {s}")
            sess.activate_skill(s)
        active_skills = [s[0] for s in sess.active_skills]

        # track initial source/destination for use in routing tests
        e_src = o_src = self.source_message[0].context.get("source")
        e_dst = o_dst = self.source_message[0].context.get("destination")

        # the capture session will store all messages until capture.finish()
        #  even if multiple messages are emitted
        capture = CaptureSession(self.minicroft, eof_msgs=self.eof_msgs,
                                 ignore_messages=self.ignore_messages)
        for idx, source_message in enumerate(self.source_message):
            if "session" not in source_message.context and len(capture.responses):
                # propagate session updates as a client would do
                source_message.context["session"] = capture.responses[-1].context["session"]
            capture.capture(source_message, timeout)

        # final message list
        messages = capture.finish()

        if self.test_message_number:
            n1 = len(self.expected_messages)
            n2 = len(messages)
            if n1 != n2:
                for i, n in enumerate(messages):
                    print("\t", i, n.serialize())
            assert n1 == n2, f"got {n2} messages, expected {n1}"


        for expected, received in zip(self.expected_messages, messages):

            # track expected active skills
            if received.msg_type in self.activation_points and "skill_id" in received.context:
                active_skills.append(received.context["skill_id"])
            if received.msg_type in self.deactivation_points and "skill_id" in received.context:
                if received.context["skill_id"] in active_skills:
                    active_skills.remove(received.context["skill_id"])

            try:
                if expected.msg_type in self.flip_points:
                    e_src = expected.context.get("source")
                    e_dst = expected.context.get("destination")

                if self.test_msg_type:
                    assert expected.msg_type == received.msg_type, f"expected message_type '{expected.msg_type}' | got '{received.msg_type}'"
                if self.test_msg_data:
                    for k, v in expected.data.items():
                        assert received.data[k] == v, f"message data mismatch for key '{k}' - expected '{v}' | got '{received.data[k]}'"
                if self.test_msg_context:
                    for k, v in expected.context.items():
                        assert received.context[k] == v
                if self.test_routing:
                    r_src = received.context.get("source")
                    r_dst = received.context.get("destination")
                    if expected.msg_type in self.keep_original_src:
                        assert o_src == r_src  # compare against original
                        assert o_dst == r_dst
                    else:
                        assert e_src == r_src  # compare against expected
                        assert e_dst == r_dst
                    if expected.msg_type in self.flip_points:
                        e_src, e_dst = e_dst, e_src

                if self.test_active_skills and active_skills:
                    sess = SessionManager.get(received)
                    skills = [s[0] for s in sess.active_skills]
                    for s in active_skills:
                        assert s in skills, f"{s} missing from active skills list"


            except Exception as e:
                print(f"Expected message: {expected.serialize()}")
                print(f"Received message: {received.serialize()}")
                raise

        if self.managed:
            self.minicroft.stop()
            del self.minicroft
            self.minicroft = None

    @staticmethod
    def anonymize_message(message: Message) -> Message:
        msg = Message(message.msg_type, message.data, message.context)
        sess = SessionManager.get(message)
        sess.location_preferences = {
            "city": {
                "code": "N/A",
                "name": "N/A",
                "state": {
                    "code": "N/A",
                    "name": "N/A",
                    "country": {
                        "code": "N/A", "name": "N/A"
                    }
                }
            },
            "coordinate": {"latitude": 0, "longitude": 0},
            "timezone": {"code": "Europe/Lisbon", "name": "Europe/Lisbon"}
        }
        msg.context["session"] = sess.serialize()
        return msg

    def serialize(self, anonymize=True) -> SerializedTest:
        src = [self.anonymize_message(m) if anonymize else m
               for m in self.source_message]
        expected = [self.anonymize_message(m) if anonymize else m
                    for m in self.expected_messages]
        data = {
            "skill_ids": self.skill_ids,
            "source_message": [json.loads(m.serialize()) for m in src],
            "expected_messages": [json.loads(m.serialize()) for m in expected],
            "eof_msgs": self.eof_msgs,
            "flip_points": self.flip_points,
            "test_msg_type": self.test_msg_type,
            "test_msg_data": self.test_msg_data,
            "test_msg_context": self.test_msg_context,
            "test_routing": self.test_routing
        }
        return data

    @staticmethod
    def deserialize(data: Union[str, SerializedTest]) -> 'End2EndTest':
        if isinstance(data, str):
            data = json.loads(data)
        kwargs = data
        kwargs["source_message"] = [Message.deserialize(m) for m in data["source_message"]]
        kwargs["expected_messages"] = [Message.deserialize(m) for m in data["expected_messages"]]
        return End2EndTest(**kwargs)

    @classmethod
    def from_message(cls, message: Union[Message, List[Message]],
                     skill_ids: List[str],
                     eof_msgs: Optional[List[str]] = None,
                     flip_points: Optional[List[str]] = None,
                     ignore_messages: Optional[List[str]] = None,
                     timeout=20) -> 'End2EndTest':
        if not isinstance(message, list):
            message = [message]
        eof_msgs = eof_msgs or DEFAULT_EOF
        flip_points = flip_points or DEFAULT_FLIP_POINTS
        ignore_messages = ignore_messages or DEFAULT_IGNORED

        minicroft = get_minicroft(skill_ids)
        capture = CaptureSession(minicroft,
                                 eof_msgs=eof_msgs,
                                 ignore_messages=ignore_messages)

        for idx, source_message in enumerate(message):
            if "session" not in source_message.context:
                # propagate session updates as a client would do
                source_message.context["session"] = capture.responses[-1].context["session"]
            capture.capture(source_message, timeout)

        minicroft.stop()
        expected_messages = capture.finish()
        return End2EndTest(
            skill_ids=skill_ids,
            source_message=message,
            expected_messages=expected_messages,
            flip_points=flip_points
        )

    @staticmethod
    def from_path(path: str) -> 'End2EndTest':
        with open(path) as f:
            return End2EndTest.deserialize(f.read())

    def save(self, path: str, anonymize=True):
        with open(path, "w") as f:
            json.dump(self.serialize(anonymize=anonymize), f, ensure_ascii=False, indent=2)


if __name__ == "__main__":
    # LOG.set_level("CRITICAL")
    session = Session("123")
    session.pipeline = ["ovos-converse-pipeline-plugin", "ovos-padatious-pipeline-plugin-high"]

    message1 = Message("recognizer_loop:utterance",
                       {"utterances": ["start parrot mode"], "lang": "en-US"},
                       {"session": session.serialize(), "source": "A", "destination": "B"})
    message2 = Message("recognizer_loop:utterance",
                       {"utterances": ["echo test"], "lang": "en-US"},
                       {"source": "A", "destination": "B"})
    message3 = Message("recognizer_loop:utterance",
                       {"utterances": ["stop parrot"], "lang": "en-US"},
                       {"source": "A", "destination": "B"})
    message4 = Message("recognizer_loop:utterance",
                       {"utterances": ["echo test"], "lang": "en-US"},
                       {"source": "A", "destination": "B"})
    autotest = End2EndTest.from_message([message1, message2, message3, message4],
                                        skill_ids=["ovos-skill-parrot.openvoiceos"])
    print(autotest)
    autotest.save("test.json")
    exit()

    session = Session("123")
    session.lang = "en-US"  # change lang, pipeline, whatever as needed
    message = Message("recognizer_loop:utterance",
                      {"utterances": ["hello world"]},
                      {"session": session.serialize(), "source": "A", "destination": "B"})
    message = End2EndTest.anonymize_message(message)  # strip any location data leaked from mycroft.conf into Session

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

    # multi message test
    test = End2EndTest(
        skill_ids=[],
        source_message=[message, message],
        expected_messages=[
            message,
            Message("mycroft.audio.play_sound", {"uri": "snd/error.mp3"}),
            Message("complete_intent_failure", {}),
            Message("ovos.utterance.handled", {}),
            message,
            Message("mycroft.audio.play_sound", {"uri": "snd/error.mp3"}),
            Message("complete_intent_failure", {}),
            Message("ovos.utterance.handled", {}),
        ]
    )
    test.execute()

    # export / import
    test.deserialize(test.serialize())  # smoke test

    t = End2EndTest.from_path("test.json")
    print(t)
