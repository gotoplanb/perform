from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # Bind address. Loopback by default and deliberately so (SPEC §7): Perform
    # owns the machine's speakers and screen, and there is no auth story yet.
    # Change this only alongside PERFORM_TOKEN.
    host: str = Field(default="127.0.0.1", alias="PERFORM_HOST")
    port: int = Field(default=8770, alias="PERFORM_PORT")

    # Optional shared secret. Unset (the default) means no auth, which is fine
    # on loopback. Set it — and every request then needs `Authorization: Bearer
    # <token>` — before exposing Perform over a tunnel or to the LAN.
    token: str | None = Field(default=None, alias="PERFORM_TOKEN")

    # --- The asset library (SPEC §5) -------------------------------------

    # Where user-supplied packs are discovered. A pack is a folder of media
    # plus a pack.toml manifest; dropping a folder in here is the whole
    # install. Scanned once at startup.
    packs_dir: str = Field(default="./packs", alias="PERFORM_PACKS_DIR")

    # The warm cache: content-addressed normalized PCM. Everything playable
    # passes through here exactly once, whether it came from a pack, from a
    # path Wander handed over, or from `say`. Rebuildable — safe to delete.
    blobs_dir: str = Field(default="./blobs", alias="PERFORM_BLOBS_DIR")
    library_db: str = Field(default="./perform-library.sqlite", alias="PERFORM_LIBRARY_DB")

    # Soft ceiling on unpinned cache entries, in megabytes. Pack assets are
    # pinned and never counted or evicted; this bounds only the just-in-time
    # material. 0 disables eviction entirely.
    cache_max_mb: int = Field(default=2048, alias="PERFORM_CACHE_MAX_MB")

    # --- Audio device ----------------------------------------------------

    # Everything in the mix graph runs at this rate/width, and ingest resamples
    # to match, so the mixer never resamples mid-callback. 48k stereo float32 is
    # what CoreAudio wants natively on this hardware; changing it means every
    # cached blob is stale, so the cache key includes it.
    sample_rate: int = Field(default=48_000, alias="PERFORM_SAMPLE_RATE")
    channels: int = Field(default=2, alias="PERFORM_CHANNELS")

    # Frames per mixer callback. 512 @ 48k is ~10.7ms — small enough that a
    # stop() is perceptually immediate, large enough that Python's per-block
    # overhead stays well under the deadline. Raise it if you hear underruns.
    block_size: int = Field(default=512, alias="PERFORM_BLOCK_SIZE")

    # PortAudio output device. Empty means the system default. The literal
    # string "null" selects NullDevice, which runs the entire mix graph and
    # discards the output — that is how the test suite and any headless run
    # exercise playback without making noise (SPEC §8).
    audio_device: str = Field(default="", alias="PERFORM_AUDIO_DEVICE")

    # --- The system voice (SPEC §6 step 1) -------------------------------

    # macOS `say` voice name, e.g. "Samantha" or "Daniel". Empty means the
    # system default from System Settings → Accessibility → Spoken Content.
    # `say -v '?'` lists what is installed. Validated at startup: an unknown
    # voice fails the boot rather than silently falling back, because two
    # participants sounding identical with no explanation is the exact failure
    # SPEC §8 calls out.
    say_voice: str = Field(default="", alias="PERFORM_SAY_VOICE")
    say_rate: int = Field(default=0, alias="PERFORM_SAY_RATE")  # words/min; 0 = leave default

    # --- Buses (SPEC §3) --------------------------------------------------

    # Bus definitions, sink routing, and the duck matrix. Reloadable at runtime
    # with SIGUSR1 (not SIGHUP — uvicorn steals SIGHUP), same as conduct's
    # pricing.yaml.
    buses_config: str = Field(default="./config/buses.yaml", alias="PERFORM_BUSES_CONFIG")

    # How long a bus may sit on an open-ended item before /status reports it
    # stalled. Only applies to buses whose resting behavior is not open-ended:
    # a backdrop bus resting on `until_replaced` is its steady state, a voice
    # bus doing the same is a hung scene (SPEC §3).
    stall_after_s: float = Field(default=30.0, alias="PERFORM_STALL_AFTER_S")

    # --- Telemetry --------------------------------------------------------

    otel_endpoint: str = Field(default="http://localhost:4317", alias="OTEL_EXPORTER_OTLP_ENDPOINT")
    otel_service_name: str = Field(default="perform", alias="OTEL_SERVICE_NAME")


@lru_cache
def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]
