"""Guard against copy regressions in the Russian UI text.

The Liquid Glass migration left several literals with a swallowed first
character ("уктивная", "удаптер", "иеть"). That class of defect shipped to
production unnoticed, so this test scans the Svelte sources for the known
broken fragments and fails before it can happen again.
"""

from pathlib import Path

FRONTEND_SRC = Path(__file__).resolve().parents[2] / "frontend" / "src"

# Fragments that previously shipped with a lost first character. None of
# them occur in healthy Russian words, so a match is always a defect.
BROKEN_FRAGMENTS = ("уктивн", "удаптер", "иеть")


def test_ui_copy_has_no_known_broken_fragments() -> None:
    offenders: list[str] = []
    for path in sorted(FRONTEND_SRC.rglob("*.svelte")):
        text = path.read_text(encoding="utf-8")
        for fragment in BROKEN_FRAGMENTS:
            if fragment in text:
                offenders.append(f"{path.relative_to(FRONTEND_SRC)}: {fragment}")
    assert not offenders, "Broken UI literals found: " + ", ".join(offenders)
