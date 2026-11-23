import unittest
from typing import Any

from jarvis_voice import telemetry


class TestVoiceTelemetry(unittest.TestCase):
    def tearDown(self) -> None:
        telemetry.bind(None)  # a leaked binding would make every later test publish

    def test_emitting_with_no_hub_is_a_no_op(self) -> None:
        # A standalone `jarvis` has no brain server in front of it; every stage must still be harmless.
        telemetry.bind(None)
        telemetry.emit("voice.stt", text="hello")

    def test_a_stage_carries_the_turn_it_belongs_to(self) -> None:
        seen: list[dict[str, Any]] = []
        telemetry.bind(lambda kind, **fields: seen.append({"kind": kind, **fields}))

        turn = telemetry.new_turn()
        telemetry.emit("voice.stt", text="check the build")
        telemetry.emit("voice.intent", intent="ADDRESSED_TO_JARVIS")

        self.assertEqual([event["kind"] for event in seen], ["voice.stt", "voice.intent"])
        self.assertTrue(all(event["turn_id"] == turn for event in seen), "one sentence is one turn")
        self.assertEqual(seen[0]["text"], "check the build")

    def test_a_long_payload_is_clipped_before_it_reaches_the_bus(self) -> None:
        seen: list[dict[str, Any]] = []
        telemetry.bind(lambda kind, **fields: seen.append(fields))

        telemetry.emit("voice.return", text="x" * (telemetry.MAX_TEXT + 500))

        self.assertEqual(len(seen[0]["text"]), telemetry.MAX_TEXT)
        self.assertTrue(seen[0]["text"].endswith("…"))

    def test_a_broken_hub_never_breaks_the_voice(self) -> None:
        def explode(kind: str, **fields: Any) -> None:
            raise RuntimeError("cockpit went away")

        telemetry.bind(explode)
        telemetry.emit("voice.tts", text="still fine")


if __name__ == "__main__":
    unittest.main()
