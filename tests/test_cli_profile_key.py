from eimemory.cli.main import _cli_profile_key


def test_cli_profile_key_strips_revision_suffix() -> None:
    assert _cli_profile_key("l5.default:v1") == "l5.default"
    assert _cli_profile_key("xiaomage:v1") == "xiaomage"
    assert _cli_profile_key("l5.default:v12") == "l5.default"


def test_cli_profile_key_keeps_lineage_and_unrelated_colons() -> None:
    assert _cli_profile_key("l5.default") == "l5.default"
    assert _cli_profile_key("profile.dynamic") == "profile.dynamic"
    assert _cli_profile_key("l5.default:latest") == "l5.default:latest"
    assert _cli_profile_key("") == ""
    assert _cli_profile_key(None) == ""
