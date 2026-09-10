# Worklog

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
