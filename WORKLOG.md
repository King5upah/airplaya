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

Not done: audio playback. The ports are bound and drained so iOS is satisfied,
but AAC-ELD and ALAC both need their codec configuration passed to a decoder
out-of-band, which is its own piece of work.
