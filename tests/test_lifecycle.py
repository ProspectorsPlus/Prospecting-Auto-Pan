"""Packet atomicity and profile authority.

The observed failure this defends against: the preview showed frame 53545
while "DECISION (this frame)" showed 53542, and the profile selector read
``generic_saturated_v0`` while the pipeline ran ``yellow_map_v0``. Both are the
same class of bug - two views of one runtime derived from different sources -
so both are fixed the same way, by deriving everything from one keyed packet.
"""

from __future__ import annotations

import random
from dataclasses import replace
from typing import Any

import pytest

from prospector_engine.contracts import PacketKind, RuntimeKey
from prospector_engine.vision import ProfileAuthority, load_profiles

LIBRARY = load_profiles()


def _key(**overrides: Any) -> RuntimeKey:
    base = {
        "run_id": "run-a",
        "coordinator_generation": 1,
        "mode_session_id": 1,
        "source_epoch": 1,
        "geometry_revision": 1,
        "profile_revision": 1,
        "frame_sequence": 10,
        "content_id": None,
    }
    base.update(overrides)
    return RuntimeKey(**base)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Runtime key ordering
# ---------------------------------------------------------------------------


def test_a_newer_frame_in_the_same_world_supersedes_an_older_one() -> None:
    assert _key(frame_sequence=11).supersedes(_key(frame_sequence=10))
    assert not _key(frame_sequence=9).supersedes(_key(frame_sequence=10))
    assert not _key(frame_sequence=10).supersedes(_key(frame_sequence=10))


@pytest.mark.parametrize(
    "field",
    [
        "coordinator_generation",
        "mode_session_id",
        "source_epoch",
        "geometry_revision",
        "profile_revision",
    ],
)
def test_a_newer_world_supersedes_however_old_its_frame(field: str) -> None:
    """A new generation's first frame must replace an old generation's last."""
    older = _key(frame_sequence=9_999)
    newer = _key(frame_sequence=1, **{field: 2})

    assert newer.supersedes(older)
    assert newer.session_key != older.session_key


@pytest.mark.parametrize(
    "field",
    [
        "coordinator_generation",
        "mode_session_id",
        "source_epoch",
        "geometry_revision",
        "profile_revision",
    ],
)
def test_a_straggler_from_an_older_world_is_refused(field: str) -> None:
    """The exact shape of the bug: a cancelled worker's late frame."""
    current = _key(frame_sequence=1, **{field: 2})
    straggler = _key(frame_sequence=9_999)

    assert not straggler.supersedes(current)


def test_a_packet_from_another_process_run_is_never_accepted() -> None:
    assert not _key(run_id="run-b").supersedes(_key(run_id="run-a"))


def test_the_session_key_excludes_only_the_per_frame_fields() -> None:
    assert _key(frame_sequence=1).session_key == _key(frame_sequence=900).session_key
    assert _key(content_id=5).session_key == _key(content_id=6).session_key


# ---------------------------------------------------------------------------
# Profile authority
# ---------------------------------------------------------------------------


def test_the_authority_starts_on_the_profile_the_pipeline_actually_uses() -> None:
    authority = ProfileAuthority(LIBRARY, "yellow_map_v0")
    assert authority.active_id == "yellow_map_v0"
    assert authority.revision == 1


def test_an_unknown_initial_id_falls_back_rather_than_crashing() -> None:
    authority = ProfileAuthority(LIBRARY, "does_not_exist")
    assert authority.active_id in LIBRARY.ids()


def test_selection_is_by_stable_id_never_by_parsing_a_label() -> None:
    authority = ProfileAuthority(LIBRARY, "yellow_map_v0")
    ids = [stable_id for stable_id, _label in authority.choices()]
    labels = [label for _id, label in authority.choices()]

    assert set(ids) == set(LIBRARY.ids())
    assert authority.request("generic_saturated_v0")
    assert not authority.request(labels[0]), "a display label is not an id"


def test_a_requested_swap_only_lands_at_a_frame_boundary() -> None:
    authority = ProfileAuthority(LIBRARY, "yellow_map_v0")

    authority.request("generic_saturated_v0")
    assert authority.active_id == "yellow_map_v0", "in-flight perception is untouched"
    assert authority.pending_id == "generic_saturated_v0"
    assert authority.revision == 1

    applied = authority.apply_pending()

    assert applied is not None and applied.profile_id == "generic_saturated_v0"
    assert authority.active_id == "generic_saturated_v0"
    assert authority.revision == 2
    assert authority.pending_id is None


def test_applying_nothing_does_not_advance_the_revision() -> None:
    authority = ProfileAuthority(LIBRARY, "yellow_map_v0")
    assert authority.apply_pending() is None
    assert authority.revision == 1


def test_requesting_the_active_profile_is_a_no_op() -> None:
    authority = ProfileAuthority(LIBRARY, "yellow_map_v0")
    assert authority.request("yellow_map_v0")
    assert authority.pending_id is None
    assert authority.apply_pending() is None
    assert authority.revision == 1


def test_a_swap_notifies_its_listener_exactly_once() -> None:
    seen: list[tuple[str, int]] = []
    authority = ProfileAuthority(
        LIBRARY, "yellow_map_v0", on_change=lambda p, r: seen.append((p.profile_id, r))
    )
    authority.request("generic_saturated_v0")
    authority.apply_pending()
    authority.apply_pending()

    assert seen == [("generic_saturated_v0", 2)]


# ---------------------------------------------------------------------------
# Pipeline-level serialization
# ---------------------------------------------------------------------------


def _pipeline(authority: ProfileAuthority) -> Any:
    from prospector_engine.navigation import PerceptionPipeline
    from prospector_engine.vision import ArrowSegmenter

    return PerceptionPipeline(segmenter=ArrowSegmenter(authority.active), profiles=authority)


def test_the_pipeline_reports_the_profile_that_actually_produced_the_frame() -> None:
    from tests.fakes import make_frame

    authority = ProfileAuthority(LIBRARY, "yellow_map_v0")
    pipeline = _pipeline(authority)
    frame = make_frame(1)

    first = pipeline.analyze(frame, map_id="m", approach_valid=False)
    assert pipeline.profile.profile_id == "yellow_map_v0"

    authority.request("generic_saturated_v0")
    # Still the old profile until a frame boundary is crossed.
    assert pipeline.profile.profile_id == "yellow_map_v0"

    second = pipeline.analyze(make_frame(2), map_id="m", approach_valid=False)
    assert pipeline.profile.profile_id == "generic_saturated_v0"
    assert pipeline.profile_revision == 2
    assert first is not second


def test_a_profile_swap_drops_the_track_and_forces_a_full_frame_pass() -> None:
    from tests.fakes import make_frame

    authority = ProfileAuthority(LIBRARY, "yellow_map_v0")
    pipeline = _pipeline(authority)
    for sequence in range(1, 4):
        pipeline.analyze(make_frame(sequence), map_id="m", approach_valid=False)
    before = pipeline.full_passes

    authority.request("generic_saturated_v0")
    pipeline.analyze(make_frame(4), map_id="m", approach_valid=False)

    assert pipeline.full_passes > before
    assert pipeline.tracker.track_id is None


def test_a_geometry_change_drops_temporal_state_rather_than_reinterpreting_it() -> None:
    from tests.fakes import make_frame, make_geometry

    authority = ProfileAuthority(LIBRARY, "yellow_map_v0")
    pipeline = _pipeline(authority)
    pipeline.analyze(make_frame(1), map_id="m", approach_valid=False)
    tracker_before = pipeline.tracker

    moved = make_geometry(size=(1024.0, 768.0))
    pipeline.analyze(make_frame(2, geometry=moved), map_id="m", approach_valid=False)

    assert pipeline.tracker is not tracker_before


# ---------------------------------------------------------------------------
# Randomized lifecycle: no mixed-key packet may ever be published
# ---------------------------------------------------------------------------


class _Screen:
    """A stand-in for the dashboard's one visible packet."""

    def __init__(self) -> None:
        self.current: RuntimeKey | None = None
        self.refused = 0
        self.accepted: list[RuntimeKey] = []

    def offer(self, key: RuntimeKey) -> None:
        if self.current is not None and not key.supersedes(self.current):
            self.refused += 1
            return
        self.current = key
        self.accepted.append(key)


@pytest.mark.parametrize("seed", range(24))
def test_randomized_lifecycle_never_shows_a_mixed_key_packet(seed: int) -> None:
    """Start, stop, profile swap, resize, source replacement, in any order.

    Late frames from a cancelled worker are injected deliberately, because that
    is exactly how the preview and the decision panel drifted apart.
    """
    rng = random.Random(seed)
    screen = _Screen()
    world = {
        "coordinator_generation": 1,
        "mode_session_id": 1,
        "source_epoch": 1,
        "geometry_revision": 1,
        "profile_revision": 1,
    }
    sequence = 0
    stragglers: list[RuntimeKey] = []

    for _ in range(200):
        action = rng.choice(
            ["frame", "frame", "frame", "start", "stop", "profile", "resize", "source", "late"]
        )
        if action == "frame":
            sequence += 1
            screen.offer(_key(frame_sequence=sequence, **world))
        elif action == "late" and stragglers:
            screen.offer(stragglers.pop(rng.randrange(len(stragglers))))
        else:
            field = {
                "start": "mode_session_id",
                "stop": "mode_session_id",
                "profile": "profile_revision",
                "resize": "geometry_revision",
                "source": "source_epoch",
            }.get(action)
            if field is None:
                continue
            # A worker that is being cancelled can still be mid-frame.
            stragglers.append(_key(frame_sequence=sequence + 1, **world))
            world[field] += 1
            if action in ("start", "stop"):
                world["coordinator_generation"] += 1
            sequence = 0

    # Every accepted packet is either strictly newer in the same world, or the
    # first packet of a strictly newer world. Nothing in between is possible.
    for previous, current in zip(screen.accepted, screen.accepted[1:], strict=False):
        assert current.supersedes(previous)
    assert screen.refused > 0, "the straggler injection must actually exercise refusal"


def test_a_terminal_packet_carries_no_actionable_command() -> None:
    """A frozen last image may stay on screen. It may never authorize input."""
    from tests.test_gui import _observation

    live = _observation()
    terminal = replace(live, packet_kind=PacketKind.TERMINAL, command=None, phase=None)

    assert terminal.packet_kind is PacketKind.TERMINAL
    assert terminal.command is None
    assert terminal.frame is live.frame, "the picture may persist"


def test_a_detail_keyword_collision_is_absorbed_rather_than_raised() -> None:
    """Journalling must never take down the run it is describing.

    ``note(stage, "reason", detail="...")`` used to raise ``TypeError`` for
    "multiple values for argument 'detail'". It raised on the coordinator
    thread, inside the handler for the very event being recorded, so a mistyped
    diagnostic safe-stopped the session instead of explaining it.
    """
    from prospector_engine.lifecycle import LifecycleJournal, LifecycleStage

    journal = LifecycleJournal()
    event = journal.note(
        LifecycleStage.LIVE_REFUSED, "focus:False", detail="Roblox is not frontmost"
    )

    assert event.detail == "Roblox is not frontmost"
    assert event.fields["note"] == "focus:False"
    row = journal.rows()[-1]
    assert row["detail"] == "Roblox is not frontmost"
    assert row["note"] == "focus:False"
