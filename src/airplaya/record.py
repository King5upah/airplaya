"""Recording a clip of the mirrored session.

Video is written without re-encoding: the H.264 the phone already sent is
copied straight into an MP4. Re-encoding would cost CPU during a live mirror
and lose quality for nothing.

Audio takes the other route. It arrives as AAC-ELD, which MP4 can technically
hold but few players will open, and the decoded PCM is already available
because it is being played. So the PCM is written to a WAV file and encoded to
AAC when the clip is finalised.

    video ──> ffmpeg (copy) ──> clip.video.mp4  ┐
                                                ├─> ffmpeg mux ──> clip.mp4
    audio PCM ──────────────> clip.audio.wav    ┘

The two streams start together, which is what keeps them roughly in sync;
`-use_wallclock_as_timestamps` gives the video its timing from arrival, since a
raw H.264 stream carries none.
"""

from __future__ import annotations

import socket
import subprocess
import sys
import threading
import time
import wave
from dataclasses import dataclass
from pathlib import Path

from airplaya.log import get_logger

log = get_logger(__name__)

AUDIO_SAMPLE_RATE = 44100
AUDIO_CHANNELS = 2
_CONNECT_TIMEOUT = 5.0


class RecordingError(RuntimeError):
    """Recording could not start, with a message worth showing the user."""


@dataclass
class Clip:
    path: Path
    duration_seconds: float
    size_bytes: int


class ClipRecorder:
    """Records the current session to an MP4. One clip at a time."""

    def __init__(self, ffmpeg_binary: str = "ffmpeg") -> None:
        self._ffmpeg = ffmpeg_binary
        self._lock = threading.Lock()

        self._path: Path | None = None
        self._video_path: Path | None = None
        self._audio_path: Path | None = None
        self._process: subprocess.Popen[bytes] | None = None
        self._socket: socket.socket | None = None
        self._wave: wave.Wave_write | None = None
        self._started_at = 0.0
        self._codec = "h264"
        self._waiting_for_keyframe = True

    # -- state -----------------------------------------------------------

    @property
    def recording(self) -> bool:
        return self._process is not None

    @property
    def elapsed_seconds(self) -> float:
        return time.monotonic() - self._started_at if self.recording else 0.0

    @property
    def path(self) -> Path | None:
        return self._path

    # -- lifecycle -------------------------------------------------------

    def start(
        self,
        path: str | Path,
        codec: str = "h264",
        with_audio: bool = True,
        audio_rate: int = AUDIO_SAMPLE_RATE,
        audio_channels: int = AUDIO_CHANNELS,
    ) -> Path:
        """Begin recording to `path`. Returns the resolved output path."""
        with self._lock:
            if self._process is not None:
                raise RecordingError("a clip is already being recorded")

            target = Path(path).expanduser()
            if target.suffix.lower() != ".mp4":
                target = target.with_suffix(".mp4")
            target.parent.mkdir(parents=True, exist_ok=True)

            self._codec = codec
            self._path = target
            self._video_path = target.with_suffix(".video.mp4")
            self._audio_path = target.with_suffix(".audio.wav") if with_audio else None
            # Video only becomes decodable from a keyframe, so the clip starts
            # at the first one rather than with a few seconds of grey.
            self._waiting_for_keyframe = True

            port = _free_port()
            command = [
                self._ffmpeg,
                "-hide_banner",
                "-loglevel",
                "error",
                "-y",
                # A raw stream has no timestamps; take them from arrival time.
                "-use_wallclock_as_timestamps",
                "1",
                "-f",
                codec,
                "-i",
                f"tcp://127.0.0.1:{port}?listen=1&listen_timeout=8000",
                "-c",
                "copy",
                "-movflags",
                "+faststart",
                str(self._video_path),
            ]
            log.info("recording video to %s", self._video_path)
            try:
                self._process = subprocess.Popen(command, stdin=subprocess.DEVNULL)
            except FileNotFoundError as exc:
                self._reset()
                raise RecordingError(
                    f"could not run {self._ffmpeg!r}; install ffmpeg to record"
                ) from exc

            self._socket = self._connect(port)
            if self._socket is None:
                self.abort()
                raise RecordingError("ffmpeg did not accept the recording connection")

            if self._audio_path is not None:
                try:
                    handle = wave.open(str(self._audio_path), "wb")
                    # The rate has to match the PCM being fed in, not a
                    # constant: the cable path plays 48 kHz, and a WAV header
                    # claiming 44.1 would make the clip play back slow.
                    handle.setnchannels(audio_channels)
                    handle.setsampwidth(2)
                    handle.setframerate(audio_rate)
                    self._wave = handle
                except OSError as exc:
                    log.warning("recording without audio: %s", exc)
                    self._audio_path = None

            self._started_at = time.monotonic()
            return target

    def _connect(self, port: int) -> socket.socket | None:
        deadline = time.monotonic() + _CONNECT_TIMEOUT
        while time.monotonic() < deadline:
            if self._process is not None and self._process.poll() is not None:
                log.error("ffmpeg exited before the recording started")
                return None
            try:
                connection = socket.create_connection(("127.0.0.1", port), timeout=1.0)
            except OSError:
                time.sleep(0.05)
                continue
            connection.settimeout(None)
            return connection
        return None

    # -- feeding ---------------------------------------------------------

    def add_video(self, annex_b: bytes, has_keyframe: bool) -> None:
        """Write Annex-B video. Ignored until the first keyframe arrives."""
        with self._lock:
            connection = self._socket
            if connection is None:
                return
            if self._waiting_for_keyframe:
                if not has_keyframe:
                    return
                self._waiting_for_keyframe = False
                log.debug("recording started at a keyframe")
            try:
                connection.sendall(annex_b)
            except OSError as exc:
                log.error("recording stopped: %s", exc)
                self._socket = None

    def add_audio(self, pcm: bytes) -> None:
        """Write interleaved 16-bit PCM in the rate `start` was given."""
        with self._lock:
            handle = self._wave
            if handle is None or self._waiting_for_keyframe:
                # Hold audio back until video starts, or the clip opens with
                # sound over a blank picture.
                return
            try:
                handle.writeframes(pcm)
            except OSError as exc:
                log.warning("audio recording stopped: %s", exc)
                self._wave = None

    # -- finishing -------------------------------------------------------

    def stop(self) -> Clip:
        """Finish the clip and return where it landed."""
        with self._lock:
            if self._process is None or self._path is None:
                raise RecordingError("no clip is being recorded")

            duration = time.monotonic() - self._started_at
            connection, self._socket = self._socket, None
            process, self._process = self._process, None
            handle, self._wave = self._wave, None
            target = self._path
            video_path = self._video_path
            audio_path = self._audio_path
            self._path = None
            self._video_path = None
            self._audio_path = None

        if connection is not None:
            try:
                connection.close()
            except OSError:
                pass
        if handle is not None:
            try:
                handle.close()
            except OSError:
                pass

        # ffmpeg needs to write the MP4 index before the file is usable.
        try:
            process.wait(timeout=15)
        except subprocess.TimeoutExpired:
            log.warning("ffmpeg did not finish writing; terminating")
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()

        clip = self._combine(target, video_path, audio_path)
        log.info(
            "clip saved: %s (%.1f s, %.1f MB)",
            clip.path,
            clip.duration_seconds,
            clip.size_bytes / 1e6,
        )
        return clip

    def _combine(
        self, target: Path, video_path: Path | None, audio_path: Path | None
    ) -> Clip:
        """Mux the video and audio parts into the final clip."""
        if video_path is None or not video_path.exists() or video_path.stat().st_size == 0:
            raise RecordingError("nothing was recorded: no video reached the file")

        has_audio = (
            audio_path is not None
            and audio_path.exists()
            # A WAV header alone is 44 bytes; anything at or below that is empty.
            and audio_path.stat().st_size > 1024
        )

        if has_audio:
            command = [
                self._ffmpeg,
                "-hide_banner",
                "-loglevel",
                "error",
                "-y",
                "-i",
                str(video_path),
                "-i",
                str(audio_path),
                "-c:v",
                "copy",
                "-c:a",
                "aac",
                "-b:a",
                "192k",
                # End with whichever stream runs out first, so the clip does
                # not finish with a frozen frame or silence.
                "-shortest",
                "-movflags",
                "+faststart",
                str(target),
            ]
        else:
            command = [
                self._ffmpeg,
                "-hide_banner",
                "-loglevel",
                "error",
                "-y",
                "-i",
                str(video_path),
                "-c",
                "copy",
                "-movflags",
                "+faststart",
                str(target),
            ]

        result = subprocess.run(command, capture_output=True)
        if result.returncode != 0 or not target.exists():
            detail = result.stderr.decode("utf-8", "replace").strip()
            raise RecordingError(f"could not write the clip: {detail or 'ffmpeg failed'}")

        for temporary in (video_path, audio_path):
            if temporary is not None:
                try:
                    temporary.unlink(missing_ok=True)
                except OSError:
                    pass

        duration = _probe_duration(self._ffmpeg, target)
        return Clip(
            path=target,
            duration_seconds=duration,
            size_bytes=target.stat().st_size,
        )

    def abort(self) -> None:
        """Stop recording and throw away the partial files."""
        with self._lock:
            connection, self._socket = self._socket, None
            process, self._process = self._process, None
            handle, self._wave = self._wave, None
            paths = [self._video_path, self._audio_path]
            self._reset()

        for closeable in (connection, handle):
            if closeable is not None:
                try:
                    closeable.close()
                except OSError:
                    pass
        if process is not None:
            process.terminate()
            try:
                process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                process.kill()
        for path in paths:
            if path is not None:
                try:
                    path.unlink(missing_ok=True)
                except OSError:
                    pass

    def _reset(self) -> None:
        self._path = None
        self._video_path = None
        self._audio_path = None
        self._process = None
        self._socket = None
        self._wave = None


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def _probe_duration(ffmpeg_binary: str, path: Path) -> float:
    """Best-effort duration, read back from the finished file."""
    probe = "ffprobe.exe" if sys.platform == "win32" else "ffprobe"
    try:
        result = subprocess.run(
            [
                probe,
                "-v",
                "error",
                "-show_entries",
                "format=duration",
                "-of",
                "default=noprint_wrappers=1:nokey=1",
                str(path),
            ],
            capture_output=True,
        )
        return float(result.stdout.decode().strip())
    except (OSError, ValueError):
        return 0.0


def default_clip_path(directory: str | Path | None = None) -> Path:
    """A dated filename in the user's Videos folder, or `directory`."""
    if directory:
        base = Path(directory).expanduser()
    else:
        base = Path.home() / "Videos" / "airplaya"
    stamp = time.strftime("%Y-%m-%d %H-%M-%S")
    return base / f"airplaya {stamp}.mp4"
