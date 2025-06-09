import dataclasses
import threading
from time import sleep
from typing import Union, List

from ovos_bus_client.message import Message
from ovos_bus_client.session import SessionManager, Session
from ovos_bus_client.util.scheduler import EventScheduler
from ovos_core.intent_services import IntentService
from ovos_core.skill_manager import SkillManager
from ovos_plugin_manager.skills import find_skill_plugins
from ovos_utils.fakebus import FakeBus
from ovos_utils.log import LOG
from ovos_utils.process_utils import ProcessState



class MiniCroft(SkillManager):
    def __init__(self, skill_ids, *args, **kwargs):
        bus = FakeBus()
        super().__init__(bus, *args, **kwargs)
        self.skill_ids = skill_ids
        self.intent_service = IntentService(self.bus)
        self.scheduler = EventScheduler(bus, schedule_file="/tmp/schetest.json")

    def load_metadata_transformers(self, cfg):
        self.intent_service.metadata_plugins.config = cfg
        self.intent_service.metadata_plugins.load_plugins()

    def load_plugin_skills(self):
        LOG.info("loading skill plugins")
        plugins = find_skill_plugins()
        for skill_id, plug in plugins.items():
            if skill_id not in self.skill_ids:
                continue
            if skill_id not in self.plugin_skills:
                self._load_plugin_skill(skill_id, plug)

    def run(self):
        """Load skills and mark core as ready to start tests"""
        self.status.set_alive()
        self.load_plugin_skills()
        self.status.set_ready()
        LOG.info("Skills all loaded!")

    def stop(self):
        super().stop()
        self.scheduler.shutdown()
        SessionManager.bus = None
        SessionManager.sessions = {}
        SessionManager.default_session = SessionManager.sessions["default"] = Session("default")


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
class End2EndTest:
    skill_ids: List[str]  # skill_ids to load during the test
    source_message: Message # starts the test
    expected_messages: List[Message] # tests are performed against message list

    # if received, end message capture
    eof_msgs: List[str] = dataclasses.field(default_factory=lambda: ["ovos.utterance.handled"])

    # messages after which source and destination flip in the message.context
    flip_points: List[str] = dataclasses.field(default_factory=lambda: ["recognizer_loop:utterance"])

    # test assertions to run
    test_session_lang: bool = True
    test_session_pipeline: bool = True
    test_msg_type: bool = True
    test_msg_data: bool = True
    test_msg_context: bool = True
    test_routing: bool = True

    def capture_messages(self, timeout=20) -> List[Message]:

        test_message = self.source_message

        responses = []
        done = threading.Event()

        def handle_message(msg: str):
            nonlocal responses
            if done.is_set():
                return
            msg = Message.deserialize(msg)
            responses.append(msg)

        def handle_end_of_test(msg: Message):
            done.set()

        minicroft = get_minicroft(self.skill_ids)

        minicroft.bus.on("message", handle_message)
        for m in self.eof_msgs:
            minicroft.bus.on(m, handle_end_of_test)

        minicroft.bus.emit(test_message)
        done.wait(timeout)

        minicroft.stop()

        return responses

    def execute(self, timeout=30):
        e_src = self.source_message.context.get("source")
        e_dst = self.source_message.context.get("destination")
        messages = self.capture_messages(timeout)
        for expected, received in zip(self.expected_messages, messages):
            sess_e = SessionManager.get(expected)
            sess_r = SessionManager.get(received)
            if self.test_msg_type:
                assert expected.msg_type == received.msg_type
            if self.test_msg_data:
                for k, v in expected.data.items():
                    assert received.data[k] == v
            if self.test_msg_context:
                for k, v in expected.context.items():
                    assert received.context[k] == v
            if self.test_routing:
                r_src = received.context.get("source")
                r_dst = received.context.get("destination")
                assert e_src == r_src
                assert e_dst == r_dst
                if expected.msg_type in self.flip_points:
                    e_src, e_dst = e_dst, e_src

            if self.test_session_lang:
                assert sess_e.lang == sess_r.lang
            if self.test_session_pipeline:
                assert sess_e.pipeline == sess_e.pipeline

if __name__ == "__main__":
    LOG.set_level("CRITICAL")

    session = Session("123") # change lang, pipeline, whatever as needed
    message = Message("recognizer_loop:utterance",
                      {"utterances": ["hello world"]},
                      {"session": session.serialize(),
                       "source": "A", "destination": "B"})

    test = End2EndTest(
        skill_ids=["skill-ovos-hello-world.openvoiceos"],
        source_message=message,
        expected_messages=[
            Message("recognizer_loop:utterance",
                    {"utterances": ["hello world"]},
                    {"session": session.serialize()}),
            Message("mycroft.audio.play_sound",
                    {"uri":"snd/error.mp3"},
                    {"session": session.serialize()}),
            Message("complete_intent_failure",
                    {},
                    {"session": session.serialize()}),
            Message("ovos.utterance.handled",
                    {},
                    {"session": session.serialize()}),
        ]
    )

    test.execute()