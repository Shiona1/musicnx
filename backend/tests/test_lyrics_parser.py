"""Pure LRC/plain parser tests: tag variants, offsets, fallback, UI helpers.

No network, database, Raspberry Pi or mpv is involved: every case is a string
literal, which is exactly the contract of ``app.lyrics.parser``.
"""

from __future__ import annotations

from app.core.models import LyricLine
from app.lyrics.parser import (
    ParsedLyrics,
    apply_offset,
    line_at,
    normalize_plain,
    parse,
    parse_lrc,
    parse_plain,
)


def test_parse_lrc_supports_all_timestamp_precisions() -> None:
    text = "[00:01]one\n[00:02.5]two\n[00:03.25]three\n[00:04.125]four\n"
    parsed = parse_lrc(text)

    assert parsed.format == "lrc"
    assert parsed.plain is None
    assert [(line.time_ms, line.text) for line in parsed.lines] == [
        (1_000, "one"),
        (2_500, "two"),
        (3_250, "three"),
        (4_125, "four"),
    ]


def test_parse_lrc_multiple_timestamps_on_one_line() -> None:
    parsed = parse_lrc("[00:10.00][00:20.00][00:30.50]chorus")

    assert [(line.time_ms, line.text) for line in parsed.lines] == [
        (10_000, "chorus"),
        (20_000, "chorus"),
        (30_500, "chorus"),
    ]


def test_parse_lrc_handles_metadata_and_offset_tag() -> None:
    text = "\n".join(
        [
            "[ar:Ночной Оркестр]",
            "[ti:Ночной эфир]",
            "[al:Mock Album]",
            "[by:someone]",
            "[offset:+500]",
            "[00:01.00]до смещения",
        ]
    )
    parsed = parse_lrc(text)

    # ID tags are not lyric lines; the file offset is folded into timestamps.
    assert [(line.time_ms, line.text) for line in parsed.lines] == [(1_500, "до смещения")]
    assert parsed.offset_ms == 500


def test_parse_lrc_negative_offset_clamps_at_zero() -> None:
    parsed = parse_lrc("[offset:-700]\n[00:00.20]early\n[00:02.00]later")

    assert [(line.time_ms, line.text) for line in parsed.lines] == [
        (0, "early"),
        (1_300, "later"),
    ]
    assert parsed.offset_ms == -700


def test_parse_lrc_sorts_lines_and_collapses_exact_duplicates() -> None:
    text = "\n".join(
        [
            "[00:20.00]second",
            "[00:10.00]first",
            "[00:20.00]second",
            "[00:10.00]first",
            "[00:10.00]same time, other text",
        ]
    )
    parsed = parse_lrc(text)

    assert [(line.time_ms, line.text) for line in parsed.lines] == [
        (10_000, "first"),
        (10_000, "same time, other text"),
        (20_000, "second"),
    ]


def test_parse_lrc_keeps_timed_empty_lines_and_skips_untimed_noise() -> None:
    text = "\n".join(
        [
            "just an untimed note",
            "[00:05.00]",
            "[00:06.00]after the gap",
        ]
    )
    parsed = parse_lrc(text)

    assert [(line.time_ms, line.text) for line in parsed.lines] == [
        (5_000, ""),
        (6_000, "after the gap"),
    ]


def test_parse_lrc_clamps_negative_and_keeps_large_timestamps() -> None:
    parsed = parse_lrc("[-00:01.00]negative\n[999:59.999]large")

    assert [line.time_ms for line in parsed.lines] == [0, 59_999_999]


def test_parse_lrc_preserves_unicode_and_whitespace_verbatim() -> None:
    text = "[00:01.00]  Привет, мир 🌍  \n[00:02.00]日本語の歌詞\n[00:03.00]café naïve"
    parsed = parse_lrc(text)

    assert parsed.lines[0].text == "  Привет, мир 🌍  "
    assert parsed.lines[1].text == "日本語の歌詞"
    assert parsed.lines[2].text == "café naïve"


def test_parse_lrc_on_plain_text_returns_no_lines() -> None:
    parsed = parse_lrc("просто текст\nбез таймкодов")

    assert parsed.format == "lrc"
    assert parsed.lines == []
    assert parsed.offset_ms == 0


def test_parse_plain_normalises_bom_crlf_and_trailing_space() -> None:
    text = "\ufeffПривет  \r\n\r\nМир \r\n\r\n"
    parsed = parse_plain(text)

    assert parsed.format == "plain"
    assert parsed.lines == []
    assert parsed.plain == "Привет\n\nМир"
    assert parsed.language == "und"
    assert parsed.offset_ms == 0


def test_normalize_plain_empty_input() -> None:
    assert normalize_plain("") == ""
    assert parse_plain("").plain == ""


def test_parse_sniffs_format_without_hint() -> None:
    lrc = parse("[00:01.00]sync me")
    plain = parse("just words\nsecond line")

    assert isinstance(lrc, ParsedLyrics)
    assert lrc.format == "lrc"
    assert len(lrc.lines) == 1
    assert plain.format == "plain"
    assert plain.plain == "just words\nsecond line"


def test_parse_honours_explicit_hint() -> None:
    forced_plain = parse("[00:01.00]sync me", "plain")
    forced_lrc = parse("no timestamps here", "lrc")

    assert forced_plain.format == "plain"
    assert forced_plain.lines == []
    assert forced_plain.plain == "[00:01.00]sync me"
    assert forced_lrc.format == "lrc"
    assert forced_lrc.lines == []


def test_line_at_returns_previous_line_index() -> None:
    lines = [
        LyricLine(time_ms=1_000, text="one"),
        LyricLine(time_ms=2_000, text="two"),
        LyricLine(time_ms=3_000, text="three"),
    ]

    assert line_at(lines, -1) is None
    assert line_at(lines, 0) is None
    assert line_at(lines, 999) is None
    assert line_at(lines, 1_000) == 0
    assert line_at(lines, 1_999) == 0
    assert line_at(lines, 2_000) == 1
    assert line_at(lines, 10_000) == 2
    assert line_at([], 5_000) is None


def test_apply_offset_shifts_and_clamps_without_mutating_input() -> None:
    lines = [
        LyricLine(time_ms=1_000, text="one"),
        LyricLine(time_ms=5_000, text="two"),
    ]

    shifted = apply_offset(lines, -2_000)
    assert [(line.time_ms, line.text) for line in shifted] == [(0, "one"), (3_000, "two")]
    assert [(line.time_ms, line.text) for line in lines] == [
        (1_000, "one"),
        (5_000, "two"),
    ]

    later = apply_offset(lines, 700)
    assert [line.time_ms for line in later] == [1_700, 5_700]
