"""The sector memory: what it remembers, how fast it forgets, and how it chooses.

This is deliberately the smallest thing that could stop the two behaviours that
make an obstacle recovery look stupid - trying the same side twice because the
side was picked from the heading both times, and flipping sides every frame
because nothing remembered that the last flip failed.

It is not a terrain model and these tests do not pretend it is one. Every
assertion is about *evidence about pushes*, which is all a single RGB frame can
honestly support.
"""

from __future__ import annotations

import pytest

from prospector_engine.traversability import TraversabilityConfig, TraversabilityMemory


def _memory(**overrides: object) -> TraversabilityMemory:
    return TraversabilityMemory(TraversabilityConfig(**overrides))  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Shape
# ---------------------------------------------------------------------------


def test_the_ring_is_centred_on_forward() -> None:
    sectors = _memory().sectors(0.0)

    centres = [sector.centre_deg for sector in sectors]
    assert len(centres) == 7
    assert 0.0 in centres, "no sector is centred on straight ahead"
    assert centres == sorted(centres)
    assert centres[0] == -centres[-1], "the ring is not symmetric"


def test_an_even_sector_count_is_refused_because_forward_would_fall_on_a_seam() -> None:
    with pytest.raises(ValueError, match="odd"):
        TraversabilityConfig(sectors=6)


def test_bearings_behind_the_character_are_not_modelled_at_all() -> None:
    memory = _memory(span_deg=210.0)

    assert memory.index_for(0.0) is not None
    assert memory.index_for(179.0) is None, "a bearing behind us was given a sector"
    memory.penalize(179.0, 0.0)
    assert all(sector.cost == 0.0 for sector in memory.sectors(0.0))


# ---------------------------------------------------------------------------
# Evidence
# ---------------------------------------------------------------------------


def test_a_failed_push_costs_its_sector_and_half_its_neighbours() -> None:
    """An obstacle is wider than one bearing, so the penalty is too."""
    memory = _memory()
    memory.penalize(60.0, 0.0)

    assert memory.cost(60.0, 0.0) == pytest.approx(1.0)
    assert memory.cost(30.0, 0.0) == pytest.approx(0.5)
    assert memory.cost(-60.0, 0.0) == pytest.approx(0.0)


def test_a_push_that_worked_pays_some_of_the_cost_back_but_not_all_of_it() -> None:
    """One lucky frame must not erase a wall."""
    memory = _memory()
    memory.penalize(60.0, 0.0)
    memory.reward(60.0, 0.0)

    assert 0.0 < memory.cost(60.0, 0.0) < 1.0


def test_cost_is_bounded_however_long_the_episode_runs() -> None:
    memory = _memory(max_cost=3.0)
    for _ in range(50):
        memory.penalize(60.0, 0.0)

    assert memory.cost(60.0, 0.0) <= 3.0


def test_evidence_decays_because_the_character_has_moved_since() -> None:
    memory = _memory(decay_s=5.0)
    memory.penalize(60.0, 0.0)

    assert memory.cost(60.0, 5.0) == pytest.approx(1.0 / 2.718, abs=0.02)
    assert memory.cost(60.0, 30.0) < 0.01


def test_reset_forgets_everything() -> None:
    memory = _memory()
    memory.penalize(60.0, 0.0)
    memory.reset()

    assert memory.cost(60.0, 0.0) == 0.0


# ---------------------------------------------------------------------------
# Choosing
# ---------------------------------------------------------------------------


def test_recent_sector_evidence_beats_the_heading() -> None:
    memory = _memory()
    memory.penalize(55.0, 0.0)

    side, why = memory.choose_side(now_s=0.1, target_error_deg=40.0)

    assert side == -1, "it stepped into the side it just proved does not work"
    assert "cost" in why


def test_once_the_evidence_has_decayed_the_heading_decides_again() -> None:
    memory = _memory(decay_s=2.0)
    memory.penalize(55.0, 0.0)

    side, why = memory.choose_side(now_s=40.0, target_error_deg=40.0)

    assert side == 1
    assert "arrow" in why


def test_with_no_sector_evidence_it_steps_toward_the_target() -> None:
    side, why = _memory().choose_side(now_s=0.0, target_error_deg=-50.0)

    assert side == -1
    assert "arrow" in why


def test_observed_slip_is_used_when_the_target_is_straight_ahead() -> None:
    """Contact has already deflected the character; following the surface is
    better than fighting it."""
    side, why = _memory().choose_side(now_s=0.0, target_error_deg=1.0, lateral_drift_norm=-0.09)

    assert side == -1
    assert "sliding" in why


def test_the_side_that_just_failed_is_never_repeated_when_nothing_else_decides() -> None:
    side, why = _memory().choose_side(now_s=0.0, target_error_deg=None, failed_side=1)

    assert side == -1
    assert "already been tried" in why


def test_choosing_is_deterministic() -> None:
    """No coin flips: the same episode replays identically, which is what makes
    the route simulations mean anything."""
    memory = _memory()
    memory.penalize(55.0, 0.0)
    answers = {
        memory.choose_side(now_s=0.2, target_error_deg=30.0, lateral_drift_norm=0.05)
        for _ in range(20)
    }

    assert len(answers) == 1
