"""The Working... wait line: frames, skip rules, and a no-op off-tty."""

from __future__ import annotations

from rn_agent.cli.working import render_working, working, working_enabled


def test_working_line_includes_the_word_and_esc():
    line = render_working(0)

    assert "Working..." in line.plain
    assert "[esc]" in line.plain


def test_working_line_highlights_ng_at_point_six_seconds():
    """The screenshot's yellow ``ng`` is the window at 0.6s, not a coincidence."""
    line = render_working(0.6)
    word = "Working..."
    start = line.plain.index(word)
    highlighted = "".join(
        line.plain[span.start : span.end]
        for span in line.spans
        if "yellow" in str(span.style) and start <= span.start < start + len(word)
    )

    assert highlighted == "ng"


def test_working_line_uses_a_custom_label():
    line = render_working(0, word="Thinking")

    assert "Thinking" in line.plain
    assert "[esc]" in line.plain


def test_working_can_omit_the_esc_hint():
    assert "[esc]" not in render_working(0, esc=False).plain


def test_working_is_a_noop_when_disabled():
    ran = False
    with working(enabled=False, listen_escape=False):
        ran = True
    assert ran is True


def test_nested_working_does_not_raise():
    with working(enabled=False), working(enabled=False):
        pass


def test_pytest_disables_the_animation():
    assert working_enabled() is False
