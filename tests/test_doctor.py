from clipagent import doctor


def test_run_checks_returns_expected_names():
    names = {c.name for c in doctor.run_checks()}
    assert {"python", "ffmpeg", "ffprobe", "git", "higgsfield", "faster-whisper"} <= names


def test_all_required_ok_ignores_optional():
    checks = [doctor.Check("a", True, ""), doctor.Check("b", False, "", required=False)]
    assert doctor.all_required_ok(checks)


def test_all_required_ok_fails_on_required():
    assert not doctor.all_required_ok([doctor.Check("a", False, "")])
