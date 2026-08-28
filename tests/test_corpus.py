"""The real-frame corpus as a regression gate.

``tests/corpus/real`` holds frames taken from the owner's recording, labelled
by a reviewer and split by contiguous sequence (``prospector_engine.corpus``).
These tests hold the detector to what it has already achieved on the **eval**
split, so a change that trades away recall or lets a false lock back in fails
here instead of in the field.

They are a regression gate, not production evidence: one session, one map,
one machine, with the previous build's overlay drawn on many arrows. E-PROF
and E-DIR-E2E remain PENDING whatever this file says (plan 7.2, 7.4).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from prospector_engine.arrow import ArrowDetector, DetectorConfig
from prospector_engine.contracts import CapturedFrame
from prospector_engine.corpus import (
    FramePrediction,
    SequenceMetrics,
    by_stratum,
    canonicalize,
    evaluate_corpus,
    heading_from_axis,
    load_corpus,
    stored_to_canonical,
)
from prospector_engine.navigation import PerceptionPipeline
from prospector_engine.vision import ArrowSegmenter, load_profiles

CORPUS_DIR = Path(__file__).parent / "corpus" / "real"

pytestmark = pytest.mark.skipif(
    not (CORPUS_DIR / "labels.json").exists(), reason="the real-frame corpus is not checked out"
)


@pytest.fixture(scope="module")
def corpus() -> object:
    return load_corpus(CORPUS_DIR)


@pytest.fixture(scope="module")
def eval_results(corpus: object) -> dict[str, SequenceMetrics]:
    profile = load_profiles().get(corpus.profile_id)  # type: ignore[attr-defined]
    assert profile is not None
    holder: dict[str, PerceptionPipeline] = {}

    def make() -> PerceptionPipeline:
        return PerceptionPipeline(
            segmenter=ArrowSegmenter(profile), detector=ArrowDetector(profile, DetectorConfig())
        )

    def predict(frame: CapturedFrame) -> FramePrediction:
        result = holder["pipeline"].analyze(frame, map_id="corpus", approach_valid=False)
        arrow = result.inputs.arrow
        assert result.timing is not None
        return FramePrediction(
            accepted=arrow.valid,
            bbox_px=arrow.bbox_px,
            heading_deg=heading_from_axis(arrow.tip_px, arrow.tail_px) if arrow.valid else None,
            track_id=arrow.track_id,
            decision=result.timing.tracking_decision,
        )

    def reset() -> None:
        holder["pipeline"] = make()

    return evaluate_corpus(corpus, predict, split="eval", reset=reset)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Corpus contract
# ---------------------------------------------------------------------------


def test_the_corpus_is_split_by_contiguous_sequence(corpus: object) -> None:
    sequences = corpus.sequences  # type: ignore[attr-defined]
    assert {sequence.split for sequence in sequences} == {"tune", "eval"}
    for sequence in sequences:
        times = [frame.source_time_s for frame in sequence.frames]
        assert times == sorted(times), f"{sequence.sequence_id} is not in time order"
    tune_ranges = [
        (
            s.source,
            min(f.source_time_s for f in s.frames),
            max(f.source_time_s for f in s.frames),
        )
        for s in corpus.split("tune")  # type: ignore[attr-defined]
    ]
    for sequence in corpus.split("eval"):  # type: ignore[attr-defined]
        for frame in sequence.frames:
            assert not any(
                source == sequence.source and low <= frame.source_time_s <= high
                for source, low, high in tune_ranges
            ), "an eval frame lies inside a tune sequence of the same source"


def test_every_frame_carries_a_positive_label_or_says_it_is_unknown(corpus: object) -> None:
    for frame in corpus.frames():  # type: ignore[attr-defined]
        if frame.unknown:
            continue
        # Absence is a label. A present arrow always has a box.
        assert frame.arrow is None or frame.arrow.bbox_px[2] > 0


def test_stored_frames_map_into_the_canonical_raster(corpus: object) -> None:
    frame = corpus.frames()[0]  # type: ignore[attr-defined]
    image = corpus.load_bgr(frame)  # type: ignore[attr-defined]
    assert image.shape == (720, 1280, 3)
    box = stored_to_canonical((0, 0, 972, 568), (972, 568))
    assert box[0] == 24 and box[2] == 1232, "uniform scale with 24 px bars each side"
    assert canonicalize(image[:568, :972]).shape == (720, 1280, 3)


def test_the_corpus_declares_its_biases(corpus: object) -> None:
    provenance = corpus.provenance  # type: ignore[attr-defined]
    assert "overlay" in provenance["known_biases"]
    assert provenance["status"].startswith("PENDING")
    assert any(
        frame.arrow is not None and frame.arrow.overlay_contact
        for frame in corpus.frames()  # type: ignore[attr-defined]
    ), "the frames that carry the previous build's outline are marked"


# ---------------------------------------------------------------------------
# Regression floors on the eval split
# ---------------------------------------------------------------------------


def test_no_arrow_strata_produce_no_acquisitions(
    eval_results: dict[str, SequenceMetrics],
) -> None:
    strata = by_stratum(eval_results)
    for name in ("no-arrow-ui", "no-arrow-sand"):
        assert strata[name].absent > 0
        assert strata[name].false_acquisitions == 0, f"{name}: locked onto something"


def test_live_event_scene_false_acquisitions_stay_at_or_below_the_measured_count(
    eval_results: dict[str, SequenceMetrics],
) -> None:
    """A known weakness, held where it was measured rather than hidden.

    The live sluice/event scene (rainbow lighting, yellow banners and bars)
    still acquires event banners and particles that sit outside any fixed
    HUD band: three of eight frames when measured. The fixed bands are
    excluded by the profile; the rest is held here at the measured count so
    it can only get better without anyone noticing.
    """
    stratum = by_stratum(eval_results)["no-arrow-live-ui"]
    assert stratum.absent >= 8
    assert stratum.false_acquisitions <= 3, (
        f"live scene false acquisitions rose to {stratum.false_acquisitions}"
    )


def test_no_false_lock_and_no_identity_switch_on_eval(
    eval_results: dict[str, SequenceMetrics],
) -> None:
    total = eval_results["__all__"]
    assert total.present >= 80
    assert total.false_locks == 0
    assert total.identity_switches == 0
    assert total.single_frame_replacements == 0


def test_recall_floors_per_stratum(eval_results: dict[str, SequenceMetrics]) -> None:
    """Floors sit below the measured values so noise cannot fail them; a real
    regression - the old detector read 52% overall - will."""
    strata = by_stratum(eval_results)
    floors = {
        "pink-crystal": 0.80,
        "purple-night": 0.60,
        "purple-pale": 0.55,
        "sand-same-colour": 0.55,
        "open-water": 0.70,
        "sand-occluded": 0.55,
    }
    for name, floor in floors.items():
        recall = strata[name].recall
        assert recall is not None and recall >= floor, f"{name}: recall {recall:.2f} < {floor}"
    overall = eval_results["__all__"].recall
    assert overall is not None and overall >= 0.72


def test_direction_sign_is_mostly_right_where_it_is_given(
    eval_results: dict[str, SequenceMetrics],
) -> None:
    """The old detector flipped half its answers. Abstaining is allowed;
    confident reversal is what this guards."""
    total = eval_results["__all__"]
    assert len(total.heading_errors) >= 20
    assert total.sign_accuracy is not None and total.sign_accuracy >= 0.85
    assert total.median_error_deg is not None and total.median_error_deg <= 15.0


def test_perception_stays_cheap_on_real_frames(corpus: object) -> None:
    """Bound the median so the 264 ms regression cannot come back unnoticed."""
    import time

    profile = load_profiles().get(corpus.profile_id)  # type: ignore[attr-defined]
    assert profile is not None
    pipeline = PerceptionPipeline(
        segmenter=ArrowSegmenter(profile), detector=ArrowDetector(profile, DetectorConfig())
    )
    sequence = corpus.split("eval")[0]  # type: ignore[attr-defined]
    costs = []
    for index, frame in enumerate(sequence.frames[:12]):
        captured = corpus.load_frame(frame, index + 1)  # type: ignore[attr-defined]
        started = time.perf_counter()
        pipeline.analyze(captured, map_id="corpus", approach_valid=False)
        costs.append((time.perf_counter() - started) * 1000.0)
    costs.sort()
    assert costs[len(costs) // 2] < 40.0, f"median perception {costs[len(costs) // 2]:.1f} ms"
