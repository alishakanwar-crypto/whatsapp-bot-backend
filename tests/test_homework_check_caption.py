"""A parent's wording for 'this work was not checked' must reach the relay."""

from app.routes.webhook import _HOMEWORK_CHECK_RE


def test_parent_wordings_trigger_the_relay():
    for caption in (
        "please check",
        "Kindly check this",
        "work not checked",
        "Maths notebook not checked",
        "work pending",
        "Pending homework",
        "hw pending",
        "unchecked work",
        "teacher is checking this?",
    ):
        assert _HOMEWORK_CHECK_RE.search(caption), caption


def test_unrelated_captions_do_not_trigger_the_relay():
    for caption in (
        "Chahat",
        "Grade 8A",
        "Share this file with Mansi Gupta",
        "cheque for the fees",
        "checkered shirt day photo",
    ):
        assert not _HOMEWORK_CHECK_RE.search(caption), caption
