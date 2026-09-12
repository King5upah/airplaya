# Worklog

## 2026-09-11 — mirroring over the cable, and a build that ships

Sharpness first, since it was outstanding: the app sink was scaling every frame
to a fixed 1080-pixel long side, so a 1920x1080 mirror arrived as 1080x607 and
the front end upscaled it back. The client now reports its drawing surface in
real pixels over the control channel and the sink scales to that, with the
ceiling moved to 1920.

Then the cable. An iPhone exposes a second protocol over USB — the one
QuickTime on a Mac uses — and it has nothing in common with AirPlay: no mDNS,
no pairing, no FairPlay, no encryption. A vendor control request
(`0x40, 0x52, wIndex=2`) makes the phone re-enumerate with a hidden USB
configuration carrying two extra bulk endpoints at subclass `0x2A`, and from
there the host drives a clock handshake and pulls H.264 and 48 kHz PCM in the
clear. Latency measures around 40 ms against 100–200 over Wi-Fi.

What made it cheap is that `sdat` in a FEED packet holds length-prefixed NAL
units — exactly what the AirPlay stream holds after decryption. So
`to_annex_b` onwards is shared: the decoder, the window, the rotation restart,
the clip recorder. What was new is the packet layer (`wired/packets.py`), the
CoreMedia serialisation (`wired/coremedia.py`), the handshake
(`wired/session.py`) and the USB plumbing (`wired/usb.py`).

Three things that would have cost hours if they were not written down
somewhere:

1. **Magics are little-endian.** `ping` reads as `gnip` in a hex dump. Every
   constant is written in reading order and reversed on the wire, because
   getting this backwards produces parsers that look right and match nothing.
2. **The phone sends nothing until it is asked**, and keeps needing asking:
   one `ASYN NEED` at the start and one after every single `FEED`. Miss one and
   the stream stalls in a way that looks like a decoder fault.
3. **`HPD1` decides the quality.** The phone renders to whatever screen size
   that dictionary claims, so the app's size picker matters on the cable
   exactly as it does over Wi-Fi.

Tested against 23 frames captured from a real phone (MIT, from
`quicktime_video_hack`, credited in `tests/data/wired/README.md`), including
byte-for-byte comparison of the `HPD1` and `HPA1` dictionaries we send — the
phone either accepts a dictionary or ignores it silently, so "close enough" is
not a useful state. 94 tests pass.

The audio player gained a PCM entry point, since the cable sends audio already
decoded at 48 kHz, and the recorder now takes the sample rate instead of
assuming 44.1 kHz — a WAV header claiming the wrong rate plays the clip back
slow.

Windows is the hard part, and it is why the Go implementation this builds on
says "I have given up on windows support". Windows does not let a user-mode
program change a USB device's active configuration; WinUSB and libusbK inherit
that, and mirroring lives on configuration N. A libusb0-style filter driver
above Apple's driver works, and UsbDk works if the library call is followed by
a raw `SET_CONFIGURATION` — its libusb backend implements the call as a no-op,
so `_select_configuration` reads the active configuration back to find out
which of the two actually took effect. Nothing is installed automatically;
the app explains the prerequisite instead.

Also: the receiver's RTSP log line was claiming the exact phrase the desktop
app watches for to find the loopback command port, so the app would try to
speak JSON to RTSP. Renamed.

Finally, `scripts/build_exe.py` freezes the receiver with PyInstaller into
`dist/airplaya/`, so a shipped copy of the app needs no Python, no PyAV, no
PortAudio and no libusb on the machine. Verified standalone: mDNS
advertisement, sockets and audio all come up from the frozen build.

Written up in `docs/wired.md`.

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
