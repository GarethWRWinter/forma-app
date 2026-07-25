"""
Voice service — ElevenLabs TTS integration.

Provides text-to-speech conversion for the AI coach,
proxying through the backend to keep API keys secure.
Uses httpx (already a project dependency) for HTTP requests.
"""

import httpx

from app.config import settings

ELEVENLABS_BASE_URL = "https://api.elevenlabs.io/v1"


def _get_headers() -> dict[str, str]:
    """Build request headers for ElevenLabs API."""
    return {
        "xi-api-key": settings.elevenlabs_api_key,
        "Content-Type": "application/json",
    }


def _get_voice_settings() -> dict:
    """Standard voice settings for Coach Forma's voice."""
    return {
        "stability": 0.6,  # Slightly varied for naturalness
        "similarity_boost": 0.8,  # Stay close to base voice
        "style": 0.3,  # Some expressiveness
        "use_speaker_boost": True,
        # ElevenLabs clamps to 0.7–1.2; we run a touch quick by default.
        "speed": max(0.7, min(1.2, settings.elevenlabs_voice_speed)),
    }


async def text_to_speech_stream(text: str):
    """
    Stream audio from ElevenLabs TTS API.

    Yields raw audio bytes (MP3 format) as they arrive.
    Uses the streaming endpoint for lowest first-byte latency.

    Args:
        text: The text to convert to speech.

    Yields:
        bytes: Raw MP3 audio chunks.
    """
    url = (
        f"{ELEVENLABS_BASE_URL}/text-to-speech/"
        f"{settings.elevenlabs_voice_id}/stream"
    )

    payload = {
        "text": text,
        "model_id": settings.elevenlabs_model_id,
        "voice_settings": _get_voice_settings(),
        "output_format": "mp3_44100_128",
    }

    async with httpx.AsyncClient(timeout=30.0) as client:
        async with client.stream(
            "POST", url, headers=_get_headers(), json=payload
        ) as response:
            response.raise_for_status()
            async for chunk in response.aiter_bytes(chunk_size=4096):
                yield chunk


async def text_to_speech(text: str) -> bytes:
    """
    Non-streaming TTS: returns complete audio bytes.

    Used for the sentence-by-sentence approach where each
    sentence is converted individually and streamed to the client.

    Args:
        text: The text to convert to speech.

    Returns:
        bytes: Complete MP3 audio data.
    """
    url = (
        f"{ELEVENLABS_BASE_URL}/text-to-speech/"
        f"{settings.elevenlabs_voice_id}"
    )

    payload = {
        "text": text,
        "model_id": settings.elevenlabs_model_id,
        "voice_settings": _get_voice_settings(),
        "output_format": "mp3_44100_128",
    }

    async with httpx.AsyncClient(timeout=30.0) as client:
        response = await client.post(
            url, headers=_get_headers(), json=payload
        )
        response.raise_for_status()
        return response.content


def is_voice_enabled() -> bool:
    """Check if ElevenLabs voice is configured and available."""
    return bool(settings.elevenlabs_api_key)
