# Worklog

## 2026-09-10 — audio quality, rotation, in-app video, repository split

Audio: the metallic buzz was padding. A decoded audio plane's buffer is larger
than the samples it holds — measured at 128 bytes per 1024-sample frame — and
the whole plane was being sent to the device, so every frame carried 32 samples
of garbage. Only the real samples are copied now. On top of that, decoding moved
off the receive thread, and a jitter buffer reorders frames by RTP sequence,
drops duplicates and late arrivals, and skips a gap rather than stalling.

The audio was silent before any of that mattered, for a duller reason: the
receive loop did a blocking read on each socket in turn, so a quiet control
socket throttled the data socket to about two packets a second. One selector
over both sockets fixed it.

Rotation: the player was only restarted when the codec changed, so rotating the
phone — which changes the picture size — killed the decoder. The picture size
now comes from parsing the SPS, and new parameter sets restart the player. The
parser is checked against real x264 output, including 1170x2532.

Video output: added `--sink app`, which decodes H.264 here and streams scaled
RGBA frames over loopback TCP to a client. That is what lets the desktop app
draw the picture itself, with its own controls instead of a player's title bar.

The desktop app moved to its own (private) repository. This repository is the
protocol implementation, and it stays open.

## 2026-09-09 — first working mirror

Built the receiver from scratch against the AirPlay v1 mirroring protocol,
using RPiPlay/UxPlay as the reference for the parts that are not documented.

Done:

* mDNS advertisement for `_airplay._tcp` and `_raop._tcp`, including the TXT
  records iOS gates the session on.
* Control channel on port 7000: `/info`, `/pair-setup`, `/pair-verify`,
  `/fp-setup`, SETUP, RECORD, TEARDOWN and the parameter methods.
* Legacy pairing (X25519 + Ed25519) and the FairPlay handshake, with the
  white-box key decryption compiled from the vendored `playfair` C sources
  (`zig cc` produces the DLL; there is no compiler dependency at runtime).
* Video: AES-CTR decryption with cross-payload keystream alignment, parameter
  set handling, length-prefixed NAL units to Annex-B, output to ffplay.
* Flutter desktop front end that runs the receiver and reports its state.
* 24 unit tests over the keystream, key derivation, NAL parsing and RTSP layer.

Three bugs cost most of the debugging time, all worth remembering:

1. **`GET /info` with a `txtAirPlay` qualifier was answered with an empty
   plist.** iOS asks for the TXT record over the control channel before doing
   anything else, and an empty answer makes it abandon the session in about a
   millisecond. From the phone the symptom is "unable to connect", which sends
   you looking at the network instead of at the response body. This was the
   actual reason mirroring never started.
2. **`SO_REUSEADDR` on Windows lets a second process bind a listening port.**
   A receiver left over from an earlier run kept serving requests while a fresh
   one started up, claimed success, and logged nothing. Fixed by using
   `SO_EXCLUSIVEADDRUSE` on Windows so a stale instance fails the bind loudly.
3. **The SRV target must be a hostname the machine's existing mDNS responder
   owns.** Windows machines with Apple software have Bonjour running on port
   5353 already; publishing our own `airplaya.local` put two responders in
   conflict. Using the real hostname avoids it.

## 2026-09-09 — audio, and the invisible video window

Audio now plays: RTP on UDP 6000 decrypted with AES-128-CBC, AAC-ELD decoded
in-process by PyAV with an AudioSpecificConfig rebuilt from SETUP, played
through PortAudio on a device the user picks in the app. A LATM wrapper was
tried first so that an ffmpeg subprocess could decode; ffmpeg's LOAS demuxer
accepts an AAC-LC config but not an ELD one, so decoding moved in-process.

The video window took much longer, because everything upstream of it worked:
the log showed the handshake completing, payloads arriving, decryption
succeeding, and ffplay running and decoding — with no window on screen. Three
separate causes, each found by bisecting flags against a synthetic stream
rather than by asking for another mirroring attempt:

1. `-fflags nobuffer` stops ffplay creating its window at all. Measured
   directly: same stream, same everything else, window handle 0 with the flag
   and a real window without it.
2. `-probesize 32 -analyzeduration 0` leaves too little data to estimate a
   frame rate, so the decoder never finishes opening. `-framerate 60` replaces
   the guess.
3. Feeding ffplay over stdin never opens a window on Windows either. The stream
   now goes over a loopback TCP connection with `?listen=1`, which ffmpeg
   treats as an ordinary URL.

Also fixed: Chocolatey's `ffplay.exe` is a shim that spawns the real binary as
a child, so killing the shim orphaned four windowless ffplay processes. The
sink resolves the real executable and falls back to `taskkill /T`.

Not done: ALAC audio. The ports are bound and drained so iOS is satisfied,
but AAC-ELD and ALAC both need their codec configuration passed to a decoder
out-of-band, which is its own piece of work.
