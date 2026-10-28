"""Recovery-pass pipeline: audio events, word-edge snapping, QA freeze/clipping policy, segment ledger, shared boundaries."""
import numpy as np
import pytest

from clipagent import audioevents as ae
from clipagent import contentflow as CF
from clipagent import cutqa, plan as P, qa


def test_audio_events_find_yelling_impacts_and_silence_gaps():
    sr = ae.SR
    rng = np.random.default_rng(1)
    quiet = lambda s: (rng.standard_normal(int(sr * s)) * 0.01).astype(np.float32)
    loud = lambda s: (rng.standard_normal(int(sr * s)) * 0.5).astype(np.float32)
    x = np.concatenate([quiet(4), loud(2), quiet(4), np.zeros(int(sr * 1.5), np.float32), quiet(4)])
    ev = ae.detect(ae.features(x))
    types = {e["type"] for e in ev}
    assert {"loud", "impact", "silence"} <= types
    s = ae.summarize(ev, 3.5, 6.5)
    assert s["loud"]["count"] >= 1 and s["loud"]["max_strength"] > 10
    assert ae.summarize(ev, 0.0, 3.0).get("loud") is None


def test_snap_moves_a_cut_out_of_a_word_and_keeps_clean_cuts():
    words = lambda media, t: [(9.8, 10.6, "payoff"), (10.7, 11.0, "end")]
    assert cutqa.snap_boundary(None, 10.2, "start", words) == pytest.approx(9.72)       # start moves to just before the word it fell in
    assert cutqa.snap_boundary(None, 10.2, "end", words) == pytest.approx(10.68)        # end moves to just after it (the payoff word finishes)
    assert cutqa.snap_boundary(None, 10.65, "start", words) == 10.65                     # between words: untouched
    a, b = cutqa.snap_boundaries(None, 10.2, 10.3, words)
    assert a < 10.2 and b > 10.3


def test_freeze_frames_are_a_creative_effect_but_dead_start_and_held_tail_fail():
    assert qa.classify_freezes([(17.2, 2.2)], 40.0) == ([], [(17.2, 2.2)])               # a mid-video freeze / hold is allowed
    assert qa.classify_freezes([(0.0, 1.5)], 40.0)[0] == [(0.0, 1.5)]                    # dead opening frame
    assert qa.classify_freezes([(38.8, 1.2)], 40.0)[0] == [(38.8, 1.2)]                  # tail held longer than 1 s
    assert qa.classify_freezes([(39.2, 0.8)], 40.0)[0] == []                             # a short end hold is fine


def test_clipping_means_many_full_scale_samples_not_one_loud_peak():
    assert qa.parse_clipped_ratio("n_samples: 100000\nhistogram_0db: 5") < qa.CLIP_RATIO
    assert qa.parse_clipped_ratio("n_samples: 100000\nhistogram_0db: 500") > qa.CLIP_RATIO
    assert qa.parse_clipped_ratio("nothing") is None


def test_adjacent_scenes_may_share_one_boundary_word_but_not_real_overlap():
    mk = lambda t, a, b: {"format": "standalone", "moments": [{"rank": 1, "title": t, "start": a, "end": b, "verified": True}]}
    assert P.validate([mk("First Scene Here", 0, 20), mk("Second Scene Here", 19.0, 40)]).ok          # 1 s straddling word at a cut
    assert not P.validate([mk("First Scene Here", 0, 20), mk("Second Scene Here", 10.0, 40)]).ok     # 10 s overlap is a real duplicate


def _rows(ids):
    return [{"segment": i, "status": "rejected_after_recovery", "reason": "x", "audio_verification": "whisper + audio events", "neighbors_inspected": ["S001"]} for i in ids]


def test_ledger_covers_every_segment_exactly_once(tmp_path):
    ids = ["S001", "S002", "S003"]
    out = CF.reconcile_ledger(ids, _rows(ids), tmp_path)
    assert out["segments"] == 3 and out["counts"]["rejected_after_recovery"] == 3 and (tmp_path / "recovery-ledger.csv").is_file()
    with pytest.raises(CF.ContentError, match="missing segments"):
        CF.reconcile_ledger(ids, _rows(ids[:2]), tmp_path)
    with pytest.raises(CF.ContentError, match="more than once"):
        CF.reconcile_ledger(ids, _rows(ids) + _rows(["S002"]), tmp_path)
    bad = _rows(ids)
    bad[0]["reason"] = ""
    with pytest.raises(CF.ContentError, match="exact reason"):
        CF.reconcile_ledger(ids, bad, tmp_path)
    merged = _rows(ids)
    merged[1]["status"] = "merged_into_scene"
    with pytest.raises(CF.ContentError, match="merged_into"):
        CF.reconcile_ledger(ids, merged, tmp_path)
    used = _rows(ids)
    used[2]["status"] = "in_standalone"
    with pytest.raises(CF.ContentError, match="output filename"):
        CF.reconcile_ledger(ids, used, tmp_path)
    used[2]["output"] = "approved_clips/standalone/continuous/run/C01/C01.mp4"
    merged[1]["merged_into"] = "S001"
    merged[2] = used[2]
    out = CF.reconcile_ledger(ids, merged, tmp_path)
    assert out["counts"]["merged_into_scene"] == 1 and out["counts"]["in_standalone"] == 1                # counts come from the rows themselves


def test_volumedetect_uses_the_last_report_and_stretched_words_are_ignored():
    txt = "[x] n_samples: 0\n[y] n_samples: 1000000\n[y] max_volume: -0.0 dB\n[y] histogram_0db: 150"
    assert qa.parse_clipped_ratio(txt) == pytest.approx(0.00015)
    assert cutqa.inside_word(128.38, [(127.08, 129.66, "Wha")]) is None                   # a 2.6 s 'word' is a timing artifact
    assert cutqa.inside_word(30.99, [(30.91, 31.21, "actually")]) is not None             # a real short word is still protected


def test_snap_drops_the_straddling_word_when_keeping_it_would_hit_a_neighbour():
    words = lambda media, t: [(30.5, 31.3, "actually")]
    free = cutqa.snap_boundaries(None, 20.0, 31.0, words)
    assert free[1] == pytest.approx(31.38)                                              # normal: the word is finished
    blocked = cutqa.snap_boundaries(None, 20.0, 31.0, words, ok=lambda a, b: b < 31.0)   # a neighbour begins at 31.0
    assert blocked[1] == pytest.approx(30.42)                                           # the word is dropped instead of cutting through it
    assert cutqa.snap_boundaries(None, 20.0, 31.0, words, ok=lambda a, b: b == 31.0)[1] == 31.0   # last resort: the event's own edge
