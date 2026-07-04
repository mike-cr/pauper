from __future__ import annotations

from collections.abc import Iterable
import io
import wave


def pcm_to_wav(
    chunks: Iterable[bytes],
    *,
    sample_rate: int,
    sample_width: int = 2,
    channels: int = 1,
) -> bytes:
    with io.BytesIO() as out:
        with wave.open(out, "wb") as wav:
            wav.setnchannels(channels)
            wav.setsampwidth(sample_width)
            wav.setframerate(sample_rate)
            for chunk in chunks:
                wav.writeframes(chunk)
        return out.getvalue()

