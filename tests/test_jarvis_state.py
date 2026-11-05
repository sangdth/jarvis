import unittest

from jarvis.state import (
    AgentTopic,
    JarvisMode,
    JarvisState,
    UserIntent,
    classify_fast_command,
    synthesize_beep_pcm,
)


class TestJarvisStateAndFilters(unittest.IsolatedAsyncioTestCase):
    def test_fast_mute_unmute_classifier(self) -> None:
        self.assertEqual(classify_fast_command("Jarvis shut up please"), UserIntent.MUTE)
        self.assertEqual(classify_fast_command("be quiet for a second"), UserIntent.MUTE)
        self.assertEqual(classify_fast_command("Okay you can talk now"), UserIntent.UNMUTE)
        self.assertEqual(classify_fast_command("   "), UserIntent.IGNORE_BACKGROUND)
        self.assertIsNone(classify_fast_command("Tell Claude Code to use Postgres"))
        self.assertIsNone(classify_fast_command("Fix the silence threshold in the VAD"))
        self.assertEqual(classify_fast_command("Don't be quiet, you can talk now"), UserIntent.UNMUTE)

    def test_whisper_noise_phrases_are_dropped(self) -> None:
        # Whisper writes these on silence; alone they must not reach the model as an answer to an approval.
        for noise in ("Okay.", "Thank you.", " you ", "Thanks!"):
            self.assertEqual(classify_fast_command(noise), UserIntent.IGNORE_BACKGROUND, noise)
        self.assertIsNone(classify_fast_command("Yes, okay"))
        self.assertIsNone(classify_fast_command("Thanks, that's great"))

    def test_beep_pcm_synthesis(self) -> None:
        pcm = synthesize_beep_pcm(freq_hz=880.0, duration_ms=100, sample_rate=16000)
        # 100ms at 16kHz, 16-bit mono = 1600 samples * 2 bytes = 3200 bytes
        self.assertEqual(len(pcm), 3200)

    async def test_jarvis_mute_beep_and_unmute_digest(self) -> None:
        spoken_messages: list[str] = []
        beeps_played: list[bytes] = []
        interrupts_called: int = 0

        async def on_speak(text: str) -> None:
            spoken_messages.append(text)

        async def on_beep(pcm: bytes) -> None:
            beeps_played.append(pcm)

        async def on_interrupt() -> None:
            nonlocal interrupts_called
            interrupts_called += 1

        state = JarvisState(on_speak=on_speak, on_beep=on_beep, on_interrupt=on_interrupt)
        self.assertEqual(state.mode, JarvisMode.VOICE)

        # 1. In VOICE mode, an agent topic is spoken immediately
        topic1 = AgentTopic(
            agent_id="claude",
            agent_name="Claude Code",
            summary="asks: Should we drop the legacy users table?",
        )
        await state.notify_topic(topic1)
        self.assertEqual(len(spoken_messages), 1)
        self.assertIn("Should we drop the legacy users table?", spoken_messages[0])
        self.assertEqual(len(beeps_played), 0)

        # 2. User says "Shut up" -> switches to BEEP_ONLY, interrupts TTS, plays confirmation beep
        await state.set_mute()
        self.assertEqual(state.mode, JarvisMode.BEEP_ONLY)
        self.assertEqual(interrupts_called, 1)
        self.assertEqual(len(beeps_played), 1)

        # 3. While muted, OpenCode finishes a task needing a decision -> Jarvis only beeps and queues
        topic2 = AgentTopic(
            agent_id="opencode-fe",
            agent_name="OpenCode",
            summary="needs approval: install Tailwind v4.",
        )
        await state.notify_topic(topic2)
        self.assertEqual(len(spoken_messages), 1)  # No new speech while muted
        self.assertEqual(len(beeps_played), 2)     # Played notification beep
        self.assertEqual(len(state.pending_topics), 1)

        # 4. User says "You can talk" -> switches back to VOICE and reads out queued digest
        digest = await state.set_unmute()
        self.assertEqual(state.mode, JarvisMode.VOICE)
        self.assertEqual(len(state.pending_topics), 0)
        self.assertIn("OpenCode", digest)
        self.assertIn("Tailwind v4", digest)
        self.assertEqual(len(spoken_messages), 2)

    async def test_finished_turn_is_retold_not_read_out(self) -> None:
        spoken: list[str] = []
        reports: list[str] = []

        async def on_speak(text: str) -> None:
            spoken.append(text)

        async def on_report(text: str) -> None:
            reports.append(text)

        state = JarvisState(on_speak=on_speak, on_report=on_report)
        done = AgentTopic("claude", "Claude Code", "## Summary\nEdited `src/app.py`.", is_report=True)
        await state.notify_topic(done)
        self.assertEqual((spoken, reports), ([], ["Claude Code: ## Summary\nEdited `src/app.py`."]))

        # Muted: the question is read out on unmute, the finished turn is retold after it
        await state.set_mute()
        await state.notify_topic(AgentTopic("opencode", "OpenCode", "asks: tests or docs?"))
        await state.notify_topic(done)
        await state.set_unmute()
        self.assertIn("OpenCode asks: tests or docs?", spoken[-1])
        self.assertNotIn("OpenCode OpenCode", spoken[-1])
        self.assertNotIn("src/app.py", spoken[-1])
        self.assertEqual(len(reports), 2)

        # A long report keeps the agent's name: the trim is per report, from the end
        await state.notify_topic(AgentTopic("claude", "Claude Code", "x" * 9000, is_report=True))
        self.assertTrue(reports[-1].startswith("Claude Code: xxx"))
        self.assertEqual(len(reports[-1]), len("Claude Code: ") + 8000)

        # A finished turn ends the agent's question, so the speech check stops hearing about it
        await state.notify_topic(AgentTopic("claude", "Claude Code", "asks: Postgres or SQLite?"))
        self.assertIn("claude", state.active_agent_context)
        state.mode = JarvisMode.VOICE
        await state.notify_topic(AgentTopic("claude", "Claude Code", "Done.", is_report=True))
        self.assertNotIn("claude", state.active_agent_context)

        # Offline (no conversation model): only the last line is spoken
        offline = JarvisState(on_speak=on_speak)
        await offline.notify_topic(done)
        self.assertEqual(spoken[-1], "Claude Code: Edited `src/app.py`.")


if __name__ == "__main__":
    unittest.main()
