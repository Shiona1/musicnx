"""P2 media fixtures: real tagged MP3/FLAC/M4A/WAV files with a cover."""

from __future__ import annotations

import shutil
import struct
import wave
from pathlib import Path

FIXTURE_MEDIA = Path(__file__).resolve().parent / "fixtures" / "media"


def _write_wav(path: Path, seconds: int = 3) -> None:
    rate = 44100
    with wave.open(str(path), "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(rate)
        frames = bytearray()
        for index in range(rate * seconds):
            sample = int(6000 * ((index // 32) % 2 * 2 - 1))
            frames.extend(struct.pack("<h", sample))
        wav.writeframes(frames)


def create_library_root(tmp_path: Path) -> Path:
    root = tmp_path / "library"
    root.mkdir()
    for name in ("song_a.mp3", "song_a.flac", "song_b.flac", "song_c.m4a"):
        shutil.copy2(FIXTURE_MEDIA / name, root / name)
    _write_wav(root / "plain.wav")
    (root / "cover.jpg").write_bytes((FIXTURE_MEDIA / "cover.jpg").read_bytes())
    (root / "ignore.txt").write_text("not audio", encoding="utf-8")
    return root
