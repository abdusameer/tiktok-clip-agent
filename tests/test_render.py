from clipagent import render as rd

MOMENT = {"start": 10.0, "end": 14.0,
          "words": [["hello", 10.1, 10.4], ["there", 10.5, 10.9], ["my", 11.6, 11.8], ["friend", 11.9, 12.5]],
          "lines": [{"text": "Hello there", "first": 0, "last": 1}, {"text": "my friend", "first": 2, "last": 3}]}


def test_caption_timing_follows_words_and_never_overlaps():
    (t1, a1, b1), (t2, a2, b2) = rd.caption_timing(MOMENT)
    assert (t1, t2) == ("Hello there", "my friend")
    assert abs(a1 - 0.06) < 1e-6 and abs(a2 - 1.56) < 1e-6
    assert b1 <= a2 and b2 <= 4.0 and b1 - a1 >= 0.6


def test_frame_exact_is_whole_frames():
    d = rd.frame_exact({"start": 0.0, "end": 7.58})
    assert abs(d * rd.FPS - round(d * rd.FPS)) < 1e-9


def test_layout_stays_in_tiktok_safe_zone():
    assert rd.BAND_Y + rd.BAND_H < rd.CAP_Y            # captions never overlap the video band
    assert rd.CAP_Y + rd.CAP_H <= 1470                  # above TikTok's bottom UI
    assert rd.KEEP_H / rd.SRC_H <= 0.875                # crop line sits above the old caption strip


def test_label_sits_inside_video_band_clear_of_captions():
    assert rd.BAND_Y < rd.LABEL_Y and rd.LABEL_Y + rd.LABEL_H <= rd.BAND_Y + rd.BAND_H + 5
    assert rd.LABEL_Y + rd.LABEL_H + rd.LABEL_RISE < rd.CAP_Y
    assert 0.2 <= rd.LABEL_FADE <= 0.35 and rd.LABEL_SHOW == 2.0
