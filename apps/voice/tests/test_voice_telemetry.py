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

    def test_summarize_context_handles_pipecat_tools_schema(self) -> None:
        from pipecat.processors.aggregators.llm_context import LLMContext

        from jarvis_voice.agent_tools import agent_tools
        from jarvis_voice.llm import summarize_context

        ctx = LLMContext(messages=[{"role": "user", "content": "check my tasks"}], tools=agent_tools())
        summary = summarize_context(ctx)
        self.assertEqual(summary["messages"], 1)
        self.assertEqual(summary["user_tail"], "check my tasks")
        self.assertIn("send_agent_task", summary["tools"])
        self.assertEmpty = summarize_context(LLMContext())["tools"]
        self.assertEqual(self.assertEmpty, "")

    def test_jarvis_llm_injects_working_context_before_last_user_without_splitting_tool_calls(self) -> None:
        from jarvis_voice.llm import JarvisLLM
        from jarvis_voice.state import WorkingContext

        working = WorkingContext()
        working.add("Clicking save closes modal", project="jarvis")
        llm = JarvisLLM(api_key="k", working_context=working, settings=JarvisLLM.Settings(model="deepseek/deepseek-chat"))
        call = {"id": "c1", "type": "function", "function": {"name": "send_agent_task", "arguments": "{}"}}
        raw_messages = [
            {"role": "system", "content": "base"},
            {"role": "user", "content": "What version of React?"},
            {"role": "assistant", "content": "", "tool_calls": [call]},
            {"role": "tool", "tool_call_id": "c1", "content": '{"status": "sent"}'},
        ]
        built = llm.build_chat_completion_params({"messages": raw_messages})["messages"]
        # Inserted right before the latest user message, leaving assistant(tool_calls) -> tool adjacent.
        self.assertEqual([m["role"] for m in built], ["system", "system", "user", "assistant", "tool"])
        self.assertIn("Clicking save closes modal", built[1]["content"])
        self.assertEqual(len(raw_messages), 4)  # caller's list untouched


if __name__ == "__main__":
    unittest.main()
