"""The readable log: does it stay readable, and does it say the useful thing?

The owner's complaint was not that there was no diagnostic output - there were
sixteen lifecycle stages, a governor trace and two event rings. It was that
none of it answered "why is nothing moving" in a sentence. So these tests are
about *legibility* as much as correctness: a log that scrolls three lines a
frame is as useless as no log at all.
"""

from __future__ import annotations

import time

from prospector_engine.plainlog import PlainLog, Topic, Verdict


def _texts(log: PlainLog) -> list[str]:
    return [line.text for line in log.lines()]


def test_a_repeated_sentence_collapses_instead_of_scrolling() -> None:
    log = PlainLog()
    for _ in range(50):
        log.passed(Topic.FORWARD, "Holding W.")

    assert _texts(log) == ["Holding W."]
    assert log.lines()[0].count == 50


def test_per_frame_chatter_cannot_push_the_story_off_the_screen() -> None:
    """The failure this exists to prevent, in its exact shape.

    The engineering ring lost the arm and refusal rows under 791 repeats of one
    status line. Here the per-frame topics are rate-capped and the once-a-run
    ones are not, so the sentence a stuck person needs survives the flood.
    """
    log = PlainLog(capacity=40)
    log.failed(Topic.CHORD, "I am not hearing any keys at all.")
    for index in range(400):
        log.note(Topic.MOTION, f"Facing {index % 90} degrees off.")

    assert "I am not hearing any keys at all." in _texts(log)


def test_a_changed_sentence_still_appears_at_once() -> None:
    """Rate-capping must drop repetition, never information."""
    log = PlainLog()
    log.passed(Topic.GATE, "Every check passed.")
    log.failed(Topic.GATE, "Roblox is not the front window.")

    assert _texts(log)[-1] == "Roblox is not the front window."


def test_a_running_stage_is_rewritten_in_place_when_it_resolves() -> None:
    log = PlainLog()
    log.working(Topic.WINDOW, "Looking for the Roblox window...")
    log.passed(Topic.WINDOW, "Found the Roblox window.")

    assert _texts(log) == ["Found the Roblox window."]


def test_elapsed_time_is_readable_and_relative() -> None:
    log = PlainLog()
    log.passed(Topic.NOTE, "Started.")
    line = log.lines()[0]

    assert line.render(log.started_at_s).startswith("+0:00 OK ")


def test_a_stop_does_not_clear_the_log_but_a_new_start_does() -> None:
    """Reading back why the last run ended is most of what it is for."""
    log = PlainLog()
    log.failed(Topic.STOP, "The pictures fell behind, so I stopped.")

    assert _texts(log)

    log.restart()
    assert _texts(log) == []


def test_failures_are_findable_without_reading_everything() -> None:
    log = PlainLog()
    log.passed(Topic.WINDOW, "Found the Roblox window.")
    log.failed(Topic.CHORD, "I am not hearing any keys. Grant Input Monitoring.")
    log.note(Topic.FORWARD, "Holding W.")

    failures = log.failures()

    assert len(failures) == 1
    assert failures[0].verdict is Verdict.FAIL
    assert "Input Monitoring" in failures[0].text


def test_the_plain_story_is_exported_with_the_trace() -> None:
    log = PlainLog()
    log.failed(Topic.GATE, "Roblox was not the front window.")
    rows = list(log.as_rows())

    assert rows[0]["kind"] == "plain"
    assert rows[0]["verdict"] == "fail"
    assert rows[0]["topic"] == "gate"
    assert rows[0]["elapsed"].startswith("+")


def test_it_is_bounded_and_never_grows_without_limit() -> None:
    log = PlainLog(capacity=20)
    for index in range(500):
        log.note(Topic.NOTE, f"line {index}")
        time.sleep(0)

    assert len(log.lines(1000)) <= 20


# ---------------------------------------------------------------------------
# Wired to the real thing
# ---------------------------------------------------------------------------


def test_the_actuator_narrates_what_it_presses_and_releases() -> None:
    from prospector_engine.movement import DesiredMovement, MovementActuator

    log = PlainLog()

    class Port:
        def key_code(self, key: object) -> int:
            return 13

        def raw_key_down(self, code: int) -> None: ...

        def raw_key_up(self, code: int) -> None: ...

        def raw_pointer_delta(self, dx: int, dy: int, held: object = None) -> None: ...

    actuator = MovementActuator(
        Port(),
        deadman=None,
        focus_probe=lambda: True,
        narrate=lambda verdict, text: log.say(Topic.FORWARD, Verdict(verdict), text),
    )
    actuator.start_watchdog()
    actuator.arm()
    try:
        actuator.apply(DesiredMovement(forward=1, reason="following the arrow"))
        actuator.release_all("stop")
    finally:
        actuator.stop_watchdog()

    story = " ".join(_texts(log))
    assert "Holding W" in story
    assert "following the arrow" in story
    assert "Released W" in story


def test_a_refused_chord_reaches_the_plain_log_in_plain_words() -> None:
    """The sentence the owner needed and never got."""
    from prospector_engine.contracts import IntentType
    from tests.test_runtime_concurrency import Harness, _cancellable_worker

    harness = Harness()
    harness.register(IntentType.START_LIVE, "live", _cancellable_worker())
    harness.start()
    try:
        harness.port.set_focus(False)
        harness.chord(IntentType.START_LIVE)
        assert harness.wait_for(
            lambda: any(
                "frontmost" in line.text for line in harness.coordinator.plain.failures()
            )
        )
    finally:
        harness.close()
