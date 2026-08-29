"""Which way it is worth trying next, remembered for a few seconds.

A single RGB frame does not carry terrain elevation, and nothing here pretends
otherwise. There is no depth, no mesh, no occupancy grid and no SLAM. What
there is: a ring of directional sectors around the character's forward
direction, each holding one decaying number that answers a much smaller
question - *the last time we pushed that way, did the character move?*

That is enough to stop the two behaviours that make an obstacle recovery look
stupid. It stops the ladder trying the same side twice because the side was
chosen from the heading error both times, and it stops it flipping sides every
frame because nothing remembered that the last flip failed.

Three properties, and they are the whole design.

**Evidence decays.** The character and the camera both move, so a penalty
earned six seconds ago is about a place that is no longer in front of us. Cost
decays exponentially towards zero on the monotonic clock, evaluated lazily so
there is nothing to tick.

**Evidence is bounded.** One sector's cost is clamped, so a long episode
against a wall cannot drive it to infinity and make the sector unusable for the
rest of the run.

**Choosing is deterministic.** :meth:`TraversabilityMemory.choose_side` reads
four ordered pieces of evidence and never a coin flip, so the same episode
replays identically - which is what makes the route simulations meaningful.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

from prospector_engine.contracts import EvidenceStatus, Provenance
from prospector_engine.heading import wrap_deg

__all__ = [
    "SectorCost",
    "TraversabilityConfig",
    "TraversabilityMemory",
]


@dataclass(frozen=True)
class TraversabilityConfig:
    """Shape and forgetfulness of the sector ring. Provisional throughout."""

    #: Odd, so one sector is centred exactly on forward. Five or seven; more
    #: than that and each sector collects too little evidence to be worth
    #: keeping, because the whole memory only lives for a few seconds.
    sectors: int = 7
    #: Total arc the ring covers, centred on forward. Behind the character is
    #: deliberately not modelled: we never choose to walk backwards except as
    #: one bounded rung of the recovery ladder, which does not consult this.
    span_deg: float = 210.0
    #: Cost added when a commanded push into a sector produced no progress.
    penalty: float = 1.0
    #: Cost removed when a push into a sector restored movement. Smaller than
    #: the penalty on purpose: one lucky frame should not erase a wall.
    reward: float = 0.5
    #: Ceiling on one sector's cost.
    max_cost: float = 3.0
    #: Time for a sector's cost to fall to 1/e of its value.
    decay_s: float = 5.0
    #: Cost difference that justifies overriding the heading-based side.
    decisive_margin: float = 0.6
    provenance: Provenance = field(
        default_factory=lambda: Provenance(
            status=EvidenceStatus.PROVISIONAL,
            source=(
                "prospector_engine/traversability.py; mission section "
                "'local traversability memory'"
            ),
            note=(
                "sector count, decay and margins are chosen bounds. Nothing here is "
                "a claim about terrain; it is a record of what movement produced."
            ),
        )
    )

    def __post_init__(self) -> None:
        if self.sectors < 3 or self.sectors % 2 == 0:
            raise ValueError(f"sectors must be odd and at least 3, got {self.sectors}")


@dataclass(frozen=True)
class SectorCost:
    """One sector, for a trace or a dashboard row."""

    index: int
    centre_deg: float
    cost: float
    observations: int

    def describe(self) -> str:
        return f"{self.centre_deg:+.0f}deg cost {self.cost:.2f} ({self.observations} obs)"


class TraversabilityMemory:
    """A short-lived, player-relative record of what movement produced.

    Not thread-safe; owned by the navigator that updates it.
    """

    def __init__(self, config: TraversabilityConfig | None = None) -> None:
        self._config = config or TraversabilityConfig()
        self.reset()

    def reset(self) -> None:
        count = self._config.sectors
        self._cost = [0.0] * count
        self._at_s = [0.0] * count
        self._observations = [0] * count

    @property
    def config(self) -> TraversabilityConfig:
        return self._config

    # -- geometry ---------------------------------------------------------
    def _width_deg(self) -> float:
        return self._config.span_deg / self._config.sectors

    def index_for(self, heading_deg: float) -> int | None:
        """Which sector a player-relative bearing falls in, or ``None`` behind."""
        config = self._config
        half = config.span_deg / 2.0
        bearing = wrap_deg(heading_deg)
        if abs(bearing) > half:
            return None
        centre = config.sectors // 2
        index = centre + math.floor(bearing / self._width_deg() + 0.5)
        return max(0, min(config.sectors - 1, index))

    def centre_deg(self, index: int) -> float:
        return (index - self._config.sectors // 2) * self._width_deg()

    # -- evidence ---------------------------------------------------------
    def _decayed(self, index: int, now_s: float) -> float:
        cost = self._cost[index]
        if cost == 0.0:
            return 0.0
        elapsed = max(0.0, now_s - self._at_s[index])
        return cost * math.exp(-elapsed / self._config.decay_s)

    def _write(self, index: int, value: float, now_s: float) -> None:
        self._cost[index] = max(0.0, min(self._config.max_cost, value))
        self._at_s[index] = now_s
        self._observations[index] += 1

    def penalize(self, heading_deg: float, now_s: float, *, weight: float = 1.0) -> None:
        """A push that way produced no progress. Costs its sector and half its
        neighbours, because an obstacle is wider than one bearing."""
        index = self.index_for(heading_deg)
        if index is None:
            return
        step = self._config.penalty * max(0.0, weight)
        self._write(index, self._decayed(index, now_s) + step, now_s)
        for neighbour in (index - 1, index + 1):
            if 0 <= neighbour < self._config.sectors:
                self._write(neighbour, self._decayed(neighbour, now_s) + step * 0.5, now_s)

    def reward(self, heading_deg: float, now_s: float, *, weight: float = 1.0) -> None:
        """A push that way restored movement. Only the sector itself."""
        index = self.index_for(heading_deg)
        if index is None:
            return
        step = self._config.reward * max(0.0, weight)
        self._write(index, self._decayed(index, now_s) - step, now_s)

    def cost(self, heading_deg: float, now_s: float) -> float:
        """Decayed cost of pushing towards a bearing. Zero when unknown."""
        index = self.index_for(heading_deg)
        return 0.0 if index is None else self._decayed(index, now_s)

    def sectors(self, now_s: float) -> tuple[SectorCost, ...]:
        """Every sector, decayed to now. For the trace and the dashboard."""
        return tuple(
            SectorCost(
                index=index,
                centre_deg=self.centre_deg(index),
                cost=round(self._decayed(index, now_s), 3),
                observations=self._observations[index],
            )
            for index in range(self._config.sectors)
        )

    # -- the decision -----------------------------------------------------
    def choose_side(
        self,
        *,
        now_s: float,
        target_error_deg: float | None,
        lateral_drift_norm: float | None = None,
        failed_side: int = 0,
        detour_bearing_deg: float = 55.0,
    ) -> tuple[int, str]:
        """Which way to step around whatever is in front. Returns side and why.

        Four pieces of evidence, in the order they are worth trusting:

        1. **Sector cost** - the only one that is about *this* obstacle. Used
           when the two sides differ by more than the decisive margin.
        2. **The heading to the target** - stepping toward the arrow keeps the
           detour useful rather than merely different.
        3. **Observed lateral slip** - if contact has already deflected the
           character along a surface, continuing that way follows the surface.
        4. **The side that just failed** - never repeat it when nothing else
           has an opinion.
        """
        left_cost = self.cost(-detour_bearing_deg, now_s)
        right_cost = self.cost(detour_bearing_deg, now_s)
        if abs(left_cost - right_cost) > self._config.decisive_margin:
            side = 1 if right_cost < left_cost else -1
            return (side, f"sector cost {left_cost:.2f} left vs {right_cost:.2f} right")

        if target_error_deg is not None and abs(wrap_deg(target_error_deg)) > 8.0:
            side = 1 if wrap_deg(target_error_deg) > 0.0 else -1
            if side != failed_side or failed_side == 0:
                return (side, f"the arrow is {wrap_deg(target_error_deg):+.0f} degrees away")

        if lateral_drift_norm is not None and abs(lateral_drift_norm) > 0.02:
            side = 1 if lateral_drift_norm > 0.0 else -1
            if side != failed_side or failed_side == 0:
                return (side, "already sliding that way along the surface")

        if failed_side != 0:
            return (-failed_side, "the other side has already been tried")
        return (1, "no local evidence either way")
