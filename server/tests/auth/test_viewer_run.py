from utils.auth.viewer_run import flag_enabled, viewer_only


def test_missing_preference_uses_the_default():
    assert flag_enabled(None, False) is False
    assert flag_enabled(None, True) is True


def test_only_an_explicit_false_turns_a_flag_off():
    assert flag_enabled("false", True) is False
    assert flag_enabled("true", False) is True
    assert flag_enabled("no", True) is False


def test_editor_and_admin_are_not_viewer_only():
    assert viewer_only(["viewer"]) is True
    assert viewer_only(["editor"]) is False
    assert viewer_only(["admin"]) is False
    assert viewer_only(["viewer", "editor"]) is False
    assert viewer_only([]) is False
