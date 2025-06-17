import dataclasses
import json
import threading
from copy import deepcopy
from time import sleep
from typing import Union, List, Dict, Any, Optional

from ovos_bus_client.message import Message
from ovos_bus_client.session import SessionManager, Session
from ovos_core.intent_services import IntentService
from ovos_core.skill_manager import SkillManager
from ovos_plugin_manager.skills import find_skill_plugins
from ovos_utils.fakebus import FakeBus
from ovos_utils.log import LOG
from ovos_utils.process_utils import ProcessState
from ovos_workshop.skills.ovos import OVOSSkill

SerializedMessage = Dict[str, Union[str, Dict[str, Any]]]
SerializedTest = Dict[str, Union[str, bool, List[str], SerializedMessage]]

DEFAULT_IGNORED = ["ovos.skills.settings_changed"]
GUI_IGNORED = ["gui.clear.namespace",
               "gui.value.set",
               "mycroft.gui.screen.close",
               "gui.page.show"]
DEFAULT_EOF = ["ovos.utterance.handled"]
DEFAULT_ENTRY_POINTS = ["recognizer_loop:utterance"]
DEFAULT_FLIP_POINTS = []
DEFAULT_KEEP_SRC = ["ovos.skills.fallback.ping"]
DEFAULT_ACTIVATION = []
DEFAULT_DEACTIVATION = ["intent.service.skills.deactivate"]


class MiniCroft(SkillManager):
    def __init__(self, skill_ids,
                 enable_installer=False,
                 enable_intent_service=True,
                 enable_event_scheduler=False,
                 enable_file_watcher=False,
                 enable_skill_api=True,
                 extra_skills: Optional[Dict[str, OVOSSkill]] = None,
                 *args, **kwargs):
        self.boot_messages: List[Message] = []
        bus = FakeBus()
        bus.on("message", self.handle_boot_message)
        self.skill_ids = skill_ids
        self.extra_skills = extra_skills or {}
        super().__init__(bus, enable_installer=enable_installer,
                         enable_skill_api=enable_skill_api,
                         enable_file_watcher=enable_file_watcher,
                         enable_intent_service=enable_intent_service,
                         enable_event_scheduler=enable_event_scheduler,
                         *args, **kwargs)

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

        for skill_id, plug in self.extra_skills.items():
            LOG.debug(f"Injected test skill: {skill_id}")
            if skill_id not in self.plugin_skills:
                self._load_plugin_skill(skill_id, plug)
                LOG.info(f"Loaded test skill: {skill_id}")

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
        self.bus.close()


def get_minicroft(skill_ids: Union[List[str], str], *args, **kwargs):
    if isinstance(skill_ids, str):
        skill_ids = [skill_ids]
    assert isinstance(skill_ids, list)
    croft = MiniCroft(skill_ids, *args, **kwargs)
    croft.start()
    while croft.status.state != ProcessState.READY:
        sleep(0.1)
    return croft


@dataclasses.dataclass()
class CaptureSession:
    minicroft: MiniCroft
    responses: List[Message] = dataclasses.field(default_factory=list)
    async_responses: List[Message] = dataclasses.field(default_factory=list)

    eof_msgs: List[str] = dataclasses.field(default_factory=lambda: DEFAULT_EOF)
    ignore_messages: List[str] = dataclasses.field(default_factory=lambda: DEFAULT_IGNORED)
    async_messages: List[str] = dataclasses.field(default_factory=list) # these come from an external thread and might come in any order
    done: threading.Event = dataclasses.field(default_factory=lambda: threading.Event())

    def handle_message(self, msg: str):
        if self.done.is_set():
            return
        msg = Message.deserialize(msg)
        if msg.msg_type in self.async_messages:
            self.async_responses.append(msg)
        elif msg.msg_type not in self.ignore_messages:
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
    skill_ids: List[str]  # skill_ids to load during the test (from skill plugins)

    ##############################
    # message content test params
    ##############################
    source_message: Union[Message, List[Message]]  # to be emitted, sequentially if a list
    expected_messages: List[Message]  # tests are performed against message list
    expected_boot_sequence: List[Message] = dataclasses.field(default_factory=list)  # check before any tests are run

    ##############################
    # message type runtime modifiers
    ##############################
    eof_msgs: List[str] = dataclasses.field(default_factory=lambda: DEFAULT_EOF) # if received, end message capture
    ignore_messages: List[str] = dataclasses.field(default_factory=lambda: DEFAULT_IGNORED) # pretend any message in this list was not emitted for testing purposes
    ignore_gui: bool = True # ignore the gui namespace bus messages, usually unwanted unless explicitly testing gui integration
    async_messages: List[str] = dataclasses.field(default_factory=list) # these come from an external thread and might come in any order, validate they are received outside the main test

    ##############################
    # message routing test params
    ##############################
    # for all messages received AFTER a flip_point, expected source and destination flip in the message.context
    flip_points: List[str] = dataclasses.field(default_factory=lambda: DEFAULT_FLIP_POINTS)
    # for all messages in entry_points list, new expected source and destination are extracted from message.context (flipped)
    entry_points: List[str] = dataclasses.field(default_factory=lambda: DEFAULT_ENTRY_POINTS)
    # for all messages in keep_original_src, expected source and destination are always compared against source_message[0]  (ignores rolling check via flip_points)
    keep_original_src: List[str] = dataclasses.field(default_factory=lambda: DEFAULT_KEEP_SRC)

    ###########################
    # active skill test params
    ###########################
    inject_active: List[str] = dataclasses.field(default_factory=list) # these skill_ids will be made active before the test runs (modifies Session from source_message[0])
    disallow_extra_active_skills: bool = False # if enabled any unexpected skill_ids that are active will fail the test
    activation_points: List[str] = dataclasses.field(default_factory=lambda: DEFAULT_ACTIVATION) # skill_id (message.context) must be active AFTER any message in activation_points
    deactivation_points:List[str] = dataclasses.field(default_factory=lambda: DEFAULT_DEACTIVATION) # skill_id (message.context) must NOT be active AFTER any message in deactivation_points
    final_session: Optional[Session] = None  # if provided, extra checks will be made against Session from last received message

    ###########################
    # sub-test configuration
    ###########################
    test_message_number: bool = True
    test_async_messages: bool = True
    test_async_message_number: bool = True
    test_boot_sequence: bool = True
    test_msg_type: bool = True
    test_msg_data: bool = True
    test_msg_context: bool = True
    test_active_skills: bool = True
    test_routing: bool = True
    test_final_session: bool = True

    ###########################
    # test runner internals
    ###########################
    verbose: bool = True
    minicroft: Optional[MiniCroft] = None
    managed: bool = False

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
                assert expected.msg_type == received.msg_type, f"❌ expected boot message_type '{expected.msg_type}' | got '{received.msg_type}'"
                if self.verbose:
                    print(f"✅ boot message type match: '{expected.msg_type}'")
                for k, v in expected.data.items():
                    assert received.data[k] == v, f"❌ boot message data mismatch for key '{k}' - expected '{v}' | got '{received.data[k]}'"
                    if self.verbose:
                        print(f"✅ boot message data match: '{k}' -> '{v}'")
                for k, v in expected.context.items():
                    assert received.context[k] == v, f"❌ boot message context mismatch for key '{k}' - expected '{v}' | got '{received.data[k]}'"
                    if self.verbose:
                        print(f"✅ boot message context match: '{k}' -> '{v}'")

        sess = SessionManager.get(self.source_message[0])
        for s in self.inject_active:
            if self.verbose:
                print(f"💡 activating skill pre-test: {s}")
            sess.activate_skill(s)
        active_skills = [s[0] for s in sess.active_skills]

        # track initial source/destination for use in routing tests
        e_src = o_src = self.source_message[0].context.get("source")
        e_dst = o_dst = self.source_message[0].context.get("destination")
        if self.verbose:
            print(f"💡 original message.context source: '{o_src}'")
            print(f"💡 original message.context destination: '{o_dst}'")

        # the capture session will store all messages until capture.finish()
        #  even if multiple messages are emitted
        capture = CaptureSession(self.minicroft, eof_msgs=self.eof_msgs,
                                 ignore_messages=self.ignore_messages,
                                 async_messages=self.async_messages)
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
                first_bad = None
                for i, n in enumerate(messages):
                    if i < len(self.expected_messages):
                        e = self.expected_messages[i]
                        if e.msg_type != n.msg_type and first_bad is None:
                            first_bad = n
                            print("⚠️ first differing message:", f"{n.msg_type} (received)", f"{e.msg_type} (expected)")
                    print("\t", i, n.serialize())
            assert n1 == n2, f"❌ got {n2} messages, expected {n1}"
            if self.verbose:
                print(f"✅ got {n1} messages as expected")

        if self.test_async_message_number:
            n1 = len(self.async_messages)
            n2 = len(capture.async_responses)
            assert n1 == n2, f"❌ got {n2} async messages, expected {n1}"
            if self.verbose:
                print(f"✅ got {n1} async messages as expected")

        if self.test_async_messages:
            async_types = [m.msg_type for m in capture.async_responses]
            for m in self.async_messages:
                assert m in async_types, f"❌ missing async message: {m}"
                if self.verbose:
                    print(f"✅ got async message '{m}' as expected")

        for expected, received in zip(self.expected_messages, messages):
            if self.verbose:
                print(f"💡 Received message: {received.serialize()}")
                print(f"> Expected message: {expected.serialize()}")

            skill_id = received.context.get("skill_id")
            # track expected active skills
            if received.msg_type in self.activation_points and "skill_id" in received.context:
                if self.verbose:
                    print(f"💡 reached activation point: '{expected.msg_type}'")
                    print(f"💡 skill MUST be active from now on: '{skill_id}'")
                active_skills.append(skill_id)
            if received.msg_type in self.deactivation_points and "skill_id" in received.context:
                if self.verbose:
                    print(f"💡 reached deactivation point: '{expected.msg_type}'")
                    print(f"💡 skill must NOT be active from now on: '{skill_id}'")
                if skill_id in active_skills:
                    active_skills.remove(skill_id)

            if expected.msg_type in self.flip_points:
                e_src = expected.context.get("source")
                e_dst = expected.context.get("destination")

            if self.test_msg_type:
                assert expected.msg_type == received.msg_type, f"❌ expected message_type '{expected.msg_type}' | got '{received.msg_type}'"
                if self.verbose:
                    print(f"✅ got expected message_type: '{expected.msg_type}'")
            if self.test_msg_data:
                for k, v in expected.data.items():
                    assert received.data[k] == v, f"❌ message data mismatch for key '{k}' - expected '{v}' | got '{received.data[k]}'"
                    if self.verbose:
                        print(f"✅ got expected message data '{k}: '{v}'")
            if self.test_msg_context:
                for k, v in expected.context.items():
                    assert received.context[k] == v, f"❌ message context mismatch for key '{k}' - expected '{v}' | got '{received.context[k]}'"
                    if self.verbose:
                        print(f"✅ got expected message context '{k}: '{v}'")
            if self.test_routing:
                r_src = received.context.get("source")
                r_dst = received.context.get("destination")
                if expected.msg_type in self.keep_original_src:
                    assert o_src == r_src, f"❌ source doesnt match! expected '{o_src}' got '{r_src}'"
                    assert o_dst == r_dst, f"❌ destination doesnt match! expected '{o_dst}' got '{r_dst}'"
                else:
                    assert e_src == r_src, f"❌ source doesnt match! expected '{e_src}' got '{r_src}'"
                    assert e_dst == r_dst, f"❌ destination doesnt match! expected '{e_dst}' got '{r_dst}'"
                if self.verbose:
                    # print(f"💡 source/destination flip point: '{expected.msg_type}'")
                    print(f"✅ message source matches: {r_src}")
                    print(f"✅ message destination matches: {r_dst}")

                if expected.msg_type in self.entry_points:
                    e_src, e_dst = r_dst, r_src
                    if self.verbose:
                        print(f"💡 source/destination entry point: '{expected.msg_type}'")
                        print(f"💡 new expected message.context source: '{e_src}'")
                        print(f"💡 new expected message.context destination: '{e_dst}'")
                elif expected.msg_type in self.flip_points:
                    e_src, e_dst = e_dst, e_src
                    if self.verbose:
                        print(f"💡 source/destination flip point: '{expected.msg_type}'")
                        print(f"💡 new expected message.context source: '{e_src}'")
                        print(f"💡 new expected message.context destination: '{e_dst}'")

            if self.test_active_skills and active_skills:
                sess = SessionManager.get(received)
                skills = [s[0] for s in sess.active_skills]
                for s in active_skills:
                    assert s in skills, f"❌ '{s}' missing from active skills list"
                    if self.verbose:
                        print(f"✅ skill active as expected: '{s}'")
                if self.disallow_extra_active_skills:
                    for s in skills:
                        assert s in active_skills, f"❌ '{s}' extra skill in active skills list"


        if self.test_final_session and self.final_session:
            last_sess = SessionManager.get(messages[-1])
            expected_sess = self.final_session
            if self.verbose:
                print(f"💡 final session: {last_sess.serialize()}")
                print(f"> expected: {expected_sess.serialize()}")
            assert {s[0] for s in last_sess.active_skills} == {s[0] for s in expected_sess.active_skills}, f"❌ final session active_skills doesn't match"
            assert sess.lang == expected_sess.lang, f"❌ final session lang doesn't match"
            assert sess.pipeline == expected_sess.pipeline, f"❌ final session pipeline doesn't match"
            assert sess.system_unit == expected_sess.system_unit, f"❌ final session system_unit doesn't match"
            assert sess.date_format == expected_sess.date_format, f"❌ final session date_format doesn't match"
            assert sess.time_format == expected_sess.time_format, f"❌ final session time_format doesn't match"
            assert sess.site_id == expected_sess.site_id, f"❌ final session site_id doesn't match"
            assert sess.session_id == expected_sess.session_id, f"❌ final session session_id doesn't match"
            assert set(sess.blacklisted_skills) == set(expected_sess.blacklisted_skills), f"❌ final session blacklisted_skills doesn't match"
            assert set(sess.blacklisted_intents) == set(expected_sess.blacklisted_intents), f"❌ final session blacklisted_intents doesn't match"
            if self.verbose:
                print(f"✅ final session matches: {expected_sess.serialize()}")

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
                     async_messages: Optional[List[str]] = None,
                     timeout=20, *args, **kwargs) -> 'End2EndTest':
        if not isinstance(message, list):
            message = [message]
        eof_msgs = eof_msgs or DEFAULT_EOF
        flip_points = flip_points or DEFAULT_FLIP_POINTS
        ignore_messages = ignore_messages or DEFAULT_IGNORED

        minicroft = get_minicroft(skill_ids, *args, **kwargs)
        capture = CaptureSession(minicroft,
                                 eof_msgs=eof_msgs,
                                 ignore_messages=ignore_messages,
                                 async_messages=async_messages)

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
