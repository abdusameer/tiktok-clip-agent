import numpy as np

from clipagent import analyze as an


def seg(i, start, end, text, flags=()):
    return {"id": i, "start": start, "end": end, "text": text, "speaker": "Speaker 1", "flags": list(flags)}


def test_policy_risk_levels():
    assert an.policy_risk("that was a nice day")[0] == "low"
    assert an.policy_risk("what the fuck shit bitch")[0] == "medium"
    assert an.policy_risk("he said a dick joke")[0] == "high"


def test_candidates_end_on_sentence_and_respect_length():
    segs = [seg(0, 0, 3, "Wait, what is that?"), seg(1, 3.2, 7, "Oh my god, no way."), seg(2, 7.2, 9, "okay then")]
    db = np.zeros(40)
    cands = an.candidates_from_segments(segs, db)
    assert cands and all(c["text"].rstrip().endswith(an.TERMINAL) and an.MIN_LEN <= c["dur"] <= an.MAX_LEN for c in cands)


def test_flagged_segments_lower_score_and_are_reported():
    base = [seg(0, 0, 4, "Wait, what is that?"), seg(1, 4.2, 8, "Oh my god, no way.")]
    flagged = [seg(0, 0, 4, "Wait, what is that?", ["TIMING_ESTIMATED"]), seg(1, 4.2, 8, "Oh my god, no way.")]
    db = np.zeros(40)
    a = an.candidates_from_segments(base, db)[0]
    b = an.candidates_from_segments(flagged, db)[0]
    assert b["score"] < a["score"] and b["flagged_segment_ids"] == [0] and b["timing_estimated_fraction"] == 0.5


def test_high_risk_never_picked():
    segs = [seg(0, 0, 8, "Wait, what is that dick thing?")]
    cands = an.candidates_from_segments(segs, np.zeros(40))
    assert an.pick_non_overlapping(cands, 5) == []


def test_non_overlapping_selection():
    segs = [seg(0, 0, 4, "Wait, what is that?"), seg(1, 4.2, 8, "Oh my god, no way."), seg(2, 8.4, 12.5, "Hold on, how?")]
    picked = an.pick_non_overlapping(an.candidates_from_segments(segs, np.zeros(60)), 5)
    for a, b in zip(picked, picked[1:]):
        assert a["end"] <= b["start"] + 0.3 * min(a["dur"], b["dur"])
