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
        "copy checked nahi",
    ):
        assert _HOMEWORK_CHECK_RE.search(caption), caption


def test_already_checked_work_is_not_sent_back_for_checking():
    for caption in (
        "homework checked",
        "work checked, thank you",
    ):
        assert not _HOMEWORK_CHECK_RE.search(caption), caption


def test_check_captions_are_never_read_as_a_name():
    from app.routes.webhook import _NON_NAME_WORDS

    import re as _re

    for caption in ("work pending", "work not checked", "pending work"):
        name_words = [
            w for w in _re.sub(r"[^\w\s]", " ", caption).split()
            if len(w) >= 2 and w.isalpha() and w.lower() not in _NON_NAME_WORDS
        ]
        assert len(name_words) < 2, caption


def test_unrelated_captions_do_not_trigger_the_relay():
    for caption in (
        "Chahat",
        "Grade 8A",
        "Share this file with Mansi Gupta",
        "cheque for the fees",
        "checkered shirt day photo",
    ):
        assert not _HOMEWORK_CHECK_RE.search(caption), caption
