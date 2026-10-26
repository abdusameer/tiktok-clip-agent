from clipagent import merge as mg

CORE1 = """00:00:00 [Speaker 1]
Hello there my friend.
00:00:02 [Speaker 1]
How are you today.
00:00:04 [Speaker 2]
Hey you there.
00:00:04 [Speaker 2]
Hey you there.
00:00:04 [Speaker 2]
Hey you there.
00:00:04 [Speaker 2]
Hey you there.
00:00:05 [Speaker 1]
Fine thanks.
"""


def rows_from(tmp_path):
    p = tmp_path / "c.txt"
    p.write_text(CORE1)
    return mg.parse_core1(p)


def test_parse(tmp_path):
    rows = rows_from(tmp_path)
    assert len(rows) == 7
    assert rows[0]["t"] == 0 and rows[0]["speaker"] == "Speaker 1" and rows[0]["tokens"] == ["hello", "there", "my", "friend"]


def test_find_runs_flags_same_second_burst(tmp_path):
    assert mg.find_runs(rows_from(tmp_path)) == [(2, 5)]


def test_count_phrase_non_overlapping():
    assert mg.count_phrase(["a", "b", "a", "b", "a"], ["a", "b"]) == 2
    assert mg.count_phrase(["x"], []) == 0


def test_align_uses_text_not_core1_clock(tmp_path):
    rows = rows_from(tmp_path)
    # real timing sits 10 s later than Core1's clock
    toks = [t for r in rows[:2] + rows[6:] for t in r["tokens"]]
    clock_t = [0, 0, 0, 0, 2, 2, 2, 2, 5, 5]
    words = [{"n": t, "start": ct + 10.0, "end": ct + 10.4, "src": "small.en"} for t, ct in zip(toks, clock_t)]
    rows = [r for i, r in enumerate(rows) if i in (0, 1, 6)]
    per_row, off = mg.align(rows, words)
    mg.time_rows(rows, words, per_row, off)
    assert rows[0]["start"] == 10.0 and rows[0]["src"] == "small.en"
    assert rows[1]["start"] == 12.0


def test_unmatched_row_is_flagged_estimated():
    rows = [{"t": 0, "speaker": "Speaker 1", "text": "zzz yyy", "tokens": ["zzz", "yyy"], "norm": "zzz yyy", "flags": []},
            {"t": 5, "speaker": "Speaker 1", "text": "alpha beta gamma delta", "tokens": ["alpha", "beta", "gamma", "delta"],
             "norm": "alpha beta gamma delta", "flags": []}]
    words = [{"n": n, "start": 5.0 + i * .3, "end": 5.2 + i * .3, "src": "small.en"} for i, n in enumerate(["alpha", "beta", "gamma", "delta"])]
    per_row, off = mg.align(rows, words)
    mg.time_rows(rows, words, per_row, off)
    assert "TIMING_ESTIMATED" in rows[0]["flags"] and rows[1]["flags"] == []


def test_speaker_unknown_rules():
    mk = lambda sp, text: {"speaker": sp, "text": text, "tokens": text.split(), "flags": []}
    rows = [mk("Speaker 1", "So you're his"), mk("Speaker 5", "wife yes indeed."), mk("Speaker 1", "Okay then."),
            mk("Speaker 3", "Yeah."), mk("Speaker 1", "Right.")]
    mg.assign_speakers(rows)
    assert [r["speaker"] for r in rows] == ["Speaker 1", "UNKNOWN", "Speaker 1", "UNKNOWN", "Speaker 1"]
    assert rows[1]["core1_speaker"] == "Speaker 5"


def test_outputs_format():
    segs = [{"id": 0, "start": 1.0, "end": 3.0, "speaker": "UNKNOWN", "text": "hi", "flags": ["X"]},
            {"id": 1, "start": 2.5, "end": 3.0, "speaker": "Speaker 1", "text": "yo", "flags": []}]
    assert "00:00:01,000 --> 00:00:02,480" in mg.to_srt(segs)
    assert "UNKNOWN: hi  (!)" in mg.to_txt(segs)


def test_count_phrase_is_fuzzy_for_three_plus_words():
    toks = "check the trim check the trim".split()
    assert mg.count_phrase(toks, "check check trim".split()) == 2
    assert mg.count_phrase(["hey"], ["hey"]) == 1
    assert mg.count_phrase("totally different words here".split(), "check check trim".split()) == 0


def test_estimated_rows_stay_between_matched_neighbours():
    mk = lambda t, tokens: {"t": t, "speaker": "Speaker 1", "text": " ".join(tokens), "tokens": tokens,
                            "norm": " ".join(tokens), "flags": []}
    rows = [mk(0, ["alpha", "beta", "gamma", "delta"]), mk(3, ["zzz"]), mk(6, ["one", "two", "three", "four"])]
    words = [{"n": n, "start": float(i) * .4, "end": i * .4 + .3, "src": "small.en"} for i, n in enumerate(["alpha", "beta", "gamma", "delta"])]
    words += [{"n": n, "start": 6 + i * .4, "end": 6.3 + i * .4, "src": "small.en"} for i, n in enumerate(["one", "two", "three", "four"])]
    per_row, off = mg.align(rows, words)
    mg.time_rows(rows, words, per_row, off)
    assert rows[0]["end"] <= rows[1]["start"] <= rows[1]["end"] <= rows[2]["start"]


def test_rapid_one_word_repeats_are_not_suspect():
    rows = [{"t": 1, "tokens": ["no"], "norm": "no"} for _ in range(5)]
    assert mg.find_runs(rows) == []
    rows = [{"t": 1, "tokens": ["no"], "norm": "no"} for _ in range(6)]
    assert mg.find_runs(rows) == [(0, 5)]
