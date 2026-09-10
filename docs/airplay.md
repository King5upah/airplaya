# How AirPlay mirroring works

Notes taken while writing this receiver, checked against a real iPhone rather
than against documentation — because there is no documentation. Apple has never
published the protocol; what follows was worked out by the people behind
[playfair][playfair], [shairport-sync][shairport], [RPiPlay][rpiplay],
[UxPlay][uxplay] and [OpenAirplay][openairplay], and confirmed here on the wire.

Every log line and every measurement quoted below came from this
implementation talking to an iPhone (`iPhone18,1`) on a home Wi-Fi network.

- [The shape of the thing](#the-shape-of-the-thing)
- [1. Discovery](#1-discovery)
- [2. The control channel](#2-the-control-channel)
- [3. Keys](#3-keys)
- [4. The video stream](#4-the-video-stream)
- [5. The audio stream](#5-the-audio-stream)
- [6. Clock sync](#6-clock-sync)
- [What this receiver does differently](#what-this-receiver-does-differently)
- [Debugging playbook](#debugging-playbook)
- [Measurements](#measurements)

---

## The shape of the thing

"AirPlay" is three protocols wearing one name:

| Name | What it carries | State here |
| --- | --- | --- |
| **RAOP** (AirTunes) | audio only, the original AirPort Express protocol | partly — the audio half of mirroring |
| **AirPlay 1 mirroring** | H.264 screen mirroring, RTSP control | implemented |
| **AirPlay 2** | buffered audio, multi-room, "remote control" channel, HomeKit pairing | not implemented |

Modern iOS still speaks AirPlay 1 mirroring to a receiver that advertises
itself as one. That is the whole reason a project like this is feasible: the
phone decides which protocol to use from the mDNS advertisement and the `/info`
response, and if you claim to be an Apple TV of a certain vintage, it obliges.

Four network conversations make up a session:

```
        iPhone                                  receiver
          │
          │  mDNS: who offers _airplay._tcp?      UDP 5353
          │ ────────────────────────────────────────────>
          │
          │  RTSP: /info, pairing, FairPlay, SETUP     TCP 7000
          │ <───────────────────────────────────────────>
          │
          │  H.264, AES-CTR encrypted                  TCP 7100
          │ ────────────────────────────────────────────>
          │
          │  AAC-ELD in RTP, AES-CBC encrypted         UDP 6000/6001
          │ ────────────────────────────────────────────>
          │
          │  NTP-ish clock exchange                    UDP (receiver polls)
          │ <───────────────────────────────────────────
```

Note the direction of the media: **the phone connects to you.** A receiver is a
server on every port. This is why a host firewall stops mirroring dead while
leaving discovery working perfectly — the advertisement is multicast and gets
through, the TCP connection does not.

---

## 1. Discovery

Two Bonjour services, both on the RTSP port:

| Service | Instance name | Purpose |
| --- | --- | --- |
| `_airplay._tcp` | `airplaya` | the entry in the iOS Screen Mirroring list |
| `_raop._tcp` | `103D0A7BF7C9@airplaya` | the audio half |

Both are required. Advertise only `_airplay._tcp` and the receiver appears in
the list but mirroring does not start. The `_raop` instance name is not free
form: it must be the device's hardware address as bare uppercase hex, an `@`,
then the display name.

### The TXT records

These are the receiver's capability declaration, and iOS gates behaviour on
them. The interesting fields:

| Field | Value used here | Why it matters |
| --- | --- | --- |
| `features` | `0x5A7FFEE6,0x0` | 64 bits of capability flags, sent as two 32-bit hex halves |
| `flags` | `0x4` | status flags |
| `model` | `AppleTV3,2` | iOS changes protocol behaviour by model. Not cosmetic |
| `srcvers` | `220.68` | source version; also gates behaviour |
| `pk` | 32-byte Ed25519 public key, hex | the receiver's identity |
| `pi` | a fixed UUID | public identifier; only needs to be stable |
| `deviceid` | `10:3D:0A:7B:F7:C9` | MAC-shaped; must not change between runs |

**Bit 27 of `features` is the one to know**: "supports legacy pairing". With it
on, the client runs `/pair-setup` and `/pair-verify`, and the X25519 secret from
that exchange is mixed into the media key. Turn the bit off and the client skips
pairing entirely — which removes about five seconds from connection setup, and
changes the key derivation, because there is no shared secret to mix in. A
receiver has to handle both cases, and cannot know which applies until it sees
whether pairing happened.

`deviceid` needs to be stable across restarts: iOS remembers receivers, and a
new device ID each run means a new entry in the user's list every time.

### The trap: two responders on one host

The SRV record points at a hostname, and something has to answer an A query for
that name. On Windows, Apple software installs the Bonjour service, so port 5353
already has a responder. Publishing your own hostname there puts two responders
in conflict: the other one answers queries for a name it does not own, and the
client cannot resolve the target.

The symptom is nasty because it looks like a network problem: the receiver
appears in the picker, and connecting fails instantly with no TCP connection
ever attempted. The fix is to make the SRV target the hostname the existing
responder already claims:

```
airplaya._airplay._tcp.local. can be reached at DESKTOP-579AU88.local.:7000
```

---

## 2. The control channel

Port 7000, RTSP/1.0 in shape. The client also sends plain HTTP/1.1 requests
down the same socket, so a receiver's parser has to be lenient: request line,
headers, then exactly `Content-Length` bytes of body. Most bodies are binary
plists (`application/x-apple-binary-plist`), which Python reads natively with
`plistlib`.

Echo the `CSeq` header back on every response. `RECORD` is the exception — the
reference implementations skip it there, and so does this one.

### The sequence, as observed

```
GET  /info          qualifier ["txtAirPlay"]   → the TXT record, as data
POST /pair-verify   step 1                     → our X25519 key + signature
POST /pair-verify   step 0                     → verify the client's signature
POST /fp-setup      16 bytes                    → 142-byte canned reply
POST /fp-setup      164 bytes                   → 32-byte reply, key material kept
SETUP               ekey, eiv, timingPort       → timingPort, eventPort
GET  /info                                      → full capabilities
RECORD                                          → mirroring is live
SETUP               streams[type=110]           → dataPort 7100
SETUP               streams[type=96]            → dataPort 6000, controlPort 6001
POST /feedback      every 2 s                   → empty 200
TEARDOWN                                        → end
```

### `GET /info` and the `txtAirPlay` qualifier

The client's *first* request asks for the TXT record over the control channel,
as a plist:

```python
{"qualifier": ["txtAirPlay"]}
```

The reply must contain the same record advertised over mDNS, as a data item
under the key `txtAirPlay`, in DNS TXT wire format — each entry a length byte
followed by `key=value`.

Answer with an empty plist and iOS abandons the session in about a millisecond,
reporting that it cannot connect. Nothing else in the log looks wrong: pairing
never starts, no media port is ever touched. This single response is the
difference between "mirroring works" and "the network is broken", and it cost
more debugging time here than everything else combined.

Without a qualifier, the same endpoint returns the full capability dictionary:
`deviceID`, `features`, `model`, `pk`, `statusFlags`, `sourceVersion`, the
`audioFormats` and `audioLatencies` arrays, and a `displays` array carrying the
screen size, `maxFPS` and `refreshRate`.

### Pairing

`/pair-setup` and `/pair-verify` implement the pre-HomeKit scheme. It is worth
being blunt about what it does: **it authenticates nobody.** The receiver
accepts any client, and the client does not meaningfully verify the receiver.

Its one lasting effect is a shared secret:

```
POST /pair-setup    32 bytes in (ignored)  → our 32-byte Ed25519 public key
POST /pair-verify   0x01 | pad | X25519 pub | Ed25519 pub
                    → our X25519 pub | AES-CTR( Ed25519-sign(ours ‖ theirs) )
POST /pair-verify   0x00 | pad | AES-CTR( their signature )
```

The signature encryption is where implementations go wrong. Both signatures
travel on **one continuous AES-CTR keystream**:

```
key = SHA-512("Pair-Verify-AES-Key" ‖ shared_secret)[:16]
iv  = SHA-512("Pair-Verify-AES-IV"  ‖ shared_secret)[:16]
```

Our own 64-byte signature consumes keystream bytes 0..64, so the client's
signature is encrypted with bytes **64..128**. Decrypt it from offset zero and
the verification fails with no clue as to why. In code that means running 64
zero bytes through the cipher before touching the client's signature.

### FairPlay

`POST /fp-setup`, twice, and it is theatre:

1. 16 bytes in. Byte 14 selects one of **four constant 142-byte replies**,
   captured from a real Apple receiver years ago.
2. 164 bytes in. The reply is a fixed 12-byte header plus 20 bytes echoed from
   the request. The whole 164-byte request is kept — it is the key material for
   the next step.

Then, in the first `SETUP`, the client sends `ekey`: 72 bytes holding the AES
session key, which is recovered with the stage-2 message using the `playfair`
white-box implementation. That is roughly half a megabyte of key tables and the
only part of this receiver that is not Python — reimplementing it is not
sensible, so it is vendored as C and compiled into a shared library.

No part of this proves anything about either party. It is a ritual iOS insists
on before it will send a pixel.

### `SETUP`

Arrives at least three times, doing different jobs:

**First — keys and timing.** Carries `ekey` (72 bytes), `eiv` (16 bytes),
`deviceID`, `model`, `name`, `timingProtocol` and `timingPort`. The receiver
answers with its own `timingPort`, and `eventPort: 0` — the event channel is
unused for mirroring, and reporting zero stops the client opening a connection
nothing would answer.

**Then once per stream**, in a `streams` array:

| `type` | Stream | Reply |
| --- | --- | --- |
| 110 | mirrored video | `dataPort` (7100) |
| 96 | audio | `dataPort` (6000), `controlPort` (6001) |

The video stream request carries `streamConnectionID`, a 64-bit value that
seeds the video keystream. The audio request carries `ct` (compression type) and
`spf` (samples per frame).

`streamConnectionID` arrives as a signed value in the plist and must be treated
as unsigned when formatted into the key derivation strings — the observed value
`-1661203439114434018` is really `16785540634595117598`.

---

## 3. Keys

Four keys, derived in sequence. Getting any step wrong produces a stream that
decrypts to plausible-looking garbage rather than an error, which is why this is
worth writing down carefully:

```
ekey (72 bytes, from SETUP)
  │
  ├── playfair_decrypt(fp-setup stage 2 message, ekey) ──> aes_key (16 bytes)
  │
  ├── if pairing happened:
  │     aes_key = SHA-512(aes_key ‖ ecdh_secret)[:16]
  │
  ├── video:
  │     key = SHA-512("AirPlayStreamKey<streamConnectionID>" ‖ aes_key)[:16]
  │     iv  = SHA-512("AirPlayStreamIV<streamConnectionID>"  ‖ aes_key)[:16]
  │     └── AES-128-CTR
  │
  └── audio:
        key = aes_key, iv = eiv (from SETUP, verbatim)
        └── AES-128-CBC, re-initialised per packet
```

Two details that are easy to miss:

* Every hash here is **SHA-512 truncated to 16 bytes**, not SHA-256. (One
  reference implementation's comment says "sha-256 hash" while calling
  `EVP_sha512`. Trust the code, not the comment.)
* The label strings have **no separator** before the stream ID:
  `AirPlayStreamKey16785540634595117598`.

---

## 4. The video stream

The client connects to TCP 7100 and sends framed packets: a 128-byte header
followed by a payload.

```
offset  size  meaning
0       4     payload size, little-endian
4       1     payload type
5       1     (part of the type field; zero in practice)
6       2     payload option / flags
8       8     NTP timestamp, when the type carries one
16      112   type-specific extras (picture geometry on parameter sets)
```

| Type | Payload |
| --- | --- |
| `0x00` | encrypted video: length-prefixed NAL units |
| `0x01` | **unencrypted** parameter sets: SPS/PPS, or VPS/SPS/PPS for HEVC |
| `0x02` | once-per-second heartbeat from older clients, no payload |
| `0x05` | a performance report, as a binary plist |

### Keystream alignment

This is the subtle part of the whole protocol, and it is where a receiver
usually breaks.

One AES-CTR keystream spans the entire session — but **each payload starts on a
16-byte block boundary**, and payloads are not multiples of 16. So a payload's
trailing partial block is decrypted as if it were a full block: the bytes needed
are used, and the **leftover keystream bytes decrypt the start of the next
payload**, which therefore begins part-way into a block.

```
payload N:   [ full blocks ][ 6 bytes ]
                             └── decrypt as a 16-byte block,
                                 keep the last 10 keystream bytes
payload N+1: [ 10 bytes XOR the leftovers ][ realign ][ full blocks ]…
```

Get this wrong and the first payload decodes, then everything after it is
noise — with the decoder reporting `number of reference frames exceeds max
(probably corrupt input)` rather than anything about decryption.

### NAL units

Each payload holds NAL units prefixed with a 4-byte big-endian **length**.
Decoders want Annex-B, where the prefix is the start code `00 00 00 01` — same
width, so the rewrite is in place and free.

The lengths are a free integrity check: if they do not tile the payload
exactly, the decryption is wrong. Worth detecting explicitly, because feeding a
decoder garbage produces confusing artefacts instead of a clear error.

### Parameter sets, and rotation

Type `0x01` payloads are not encrypted, and hold the SPS and PPS in an
avcC-style layout: 6 bytes of header, a 2-byte SPS length, the SPS, a PPS count
byte, a 2-byte PPS length, the PPS.

They must be **prepended to the next video payload**, not sent on their own: a
decoder that receives parameter sets separated from the IDR they describe may
discard them.

Rotating the phone changes the picture size, and a running decoder cannot
follow that — it fails, and the window dies with it. New parameter sets are the
signal, and the picture size can be read out of the SPS (exp-Golomb parsing,
including the cropping fields; a phone's 1170x2532 is not a multiple of 16 in
either direction). On a change, restart the player.

---

## 5. The audio stream

RTP on UDP 6000, control on 6001. A 12-byte RTP header, then the payload.

**Encryption is AES-128-CBC, and the cipher state does not carry between
packets**: every packet starts from the session IV, and only whole 16-byte
blocks are encrypted — any trailing bytes are already plaintext.

### The format, and what is missing

Mirroring uses **AAC-ELD at 44.1 kHz, stereo, 480 samples per frame** (`ct = 8`,
`spf = 480`). ALAC (`ct = 2`) exists in the protocol; mirroring does not use it.

The payloads are bare AAC access units. Nothing in them says which AAC flavour,
sample rate or channel count applies — that lives in the
`AudioSpecificConfig`, and **AirPlay never transmits it**, because both ends
already know the format from `SETUP`. A receiver has to reconstruct it:

```
AAC-ELD, 44.1 kHz, stereo, 480-sample frames  ->  f8 e8 50 00
```

Bit by bit: object type escape (31) plus 7 for type 39, sample rate index 4,
two channels, the 480-sample flag, three error-resilience flags off, no
low-delay SBR, then `ELDEXT_TERM`.

That requirement has a practical consequence: **the ffmpeg command line cannot
decode this stream.** There is no flag for supplying extradata, and ADTS — the
usual way to carry AAC configuration in a byte stream — cannot describe ELD.
Wrapping the frames in LOAS/LATM, which *can* carry the config inline, gets as
far as ffmpeg recognising the stream and no further: its LOAS demuxer accepts an
AAC-LC config (`Stream #0:0: Audio: aac_latm (LC), stereo`) and rejects an ELD
one (`0 channels, unspecified sample rate`). Decoding has to happen in-process,
against a library that takes extradata directly.

Before the first real frame, an AAC-ELD stream sends packets whose entire
payload is `00 68 34 00`. They are placeholders and must not reach the decoder.

### Jitter and loss

UDP over Wi-Fi reorders and drops packets, and AAC-ELD is unforgiving: a frame
decoded out of order is noise, and a missing frame leaves the decoder
mid-stream. A few frames of reordering slack — three frames is about 33 ms —
plus duplicate rejection and an explicit "declare it lost and skip ahead" path
is the difference between clean audio and crackling.

---

## 6. Clock sync

The receiver is the client here, which is counter-intuitive: it sends 32-byte
requests to the port the phone gave in `SETUP`, and the phone replies.

```
0     0x80        RTP marker
1     0xd2        payload type (0xd3 in the reply)
2-3   0x0007      sequence
8-15  the client's reference time, echoed from the last reply
16-23 our receive time for that reply
24-31 our send time
```

Times are 64-bit NTP timestamps: seconds since 1900 in the high half,
fraction in the low half. The exchange drives audio/video sync, which this
receiver does not attempt — but the phone notices a receiver that never asks,
so the conversation is worth keeping up.

---

## What this receiver does differently

Deviations from what a real Apple TV does, and from the reference
implementations, with reasons:

**Protocol scope.** No AirPlay 2 "remote control" channel, no HLS/`airplay-video`
(the path used for playing a video file rather than mirroring), no event port,
no password or PIN authentication, no multiple simultaneous clients. An Apple TV
supports up to twelve clients, giving each a distinct session and secret; this
receiver serves one.

**No ALAC.** Recognised and skipped. Playing it needs the codec's magic cookie
from a 44-byte format packet this receiver does not parse.

**Capability values are borrowed.** `features`, `model`, `srcvers` and the
`audioFormats` bitmasks are the values UxPlay uses, which are values a real
Apple TV used. They are load-bearing but not understood field by field.

**Audio decoded in-process.** PyAV rather than an ffmpeg subprocess, for the
extradata reason above; PortAudio rather than ffplay for output, because it can
be pointed at a specific device — which is what makes "which speakers" a
setting the user can change.

**Video display, twice over.** Two output paths, and both taught us something:

* *ffplay.* Works, with caveats that cost real time to find. `-fflags nobuffer`
  stops it creating a window at all — it decodes the stream and displays
  nothing. `-probesize 32 -analyzeduration 0` leaves too little data to estimate
  a frame rate, so the decoder never finishes opening. And feeding it over
  **stdin** never opens a window on Windows either; a loopback TCP URL
  (`tcp://…?listen=1`) does.
* *In-app.* Decode here with PyAV, scale to RGBA, and stream frames to the
  desktop app over loopback TCP so the window and its controls are ours.

**Recording copies the video.** The H.264 the phone already sent goes into the
MP4 unmodified — no re-encoding, so recording costs no CPU and loses no quality.
Audio is the exception: it is re-encoded to AAC from the PCM that already exists
for playback, because AAC-ELD in MP4 is legal but poorly supported by players.

**`SO_EXCLUSIVEADDRUSE`, not `SO_REUSEADDR`.** On Windows, address reuse lets a
*second* process bind a port that is already listening, and the OS then delivers
connections to only one of them. A leftover receiver from an earlier run keeps
serving while a fresh one starts up, reports success, and logs nothing. Windows
needs the exclusive flag for a stale instance to fail the bind loudly instead.

### Security, plainly

If you run this, understand what you are running:

* **Anyone on the network can mirror to it.** Pairing authenticates nobody and
  FairPlay proves nothing. There is no password support here.
* **The control channel has no authentication** and takes commands that write
  files. It binds to `127.0.0.1` only, and must stay that way.
* **The FairPlay key tables are someone else's.** They are in every open AirPlay
  receiver, and their legal status is what it is.

---

## Debugging playbook

Symptoms map to causes much more tightly than they first appear. From this
project's own bugs:

| Symptom | Cause |
| --- | --- |
| Appears in the iOS list, "unable to connect" **instantly** | `GET /info` with the `txtAirPlay` qualifier answered with an empty plist |
| Appears in the list, connection fails, **no TCP arrives** | host firewall, or an mDNS hostname conflict with another responder |
| Connects, pairs, then `pair-verify` fails | the keystream offset: the client's signature is at bytes 64..128 |
| First payload decodes, everything after is noise | keystream alignment across payload boundaries |
| NAL lengths do not tile the payload | wrong media key — check the SHA-512 mix with the pairing secret |
| Video flows, decoder runs, **no window** | ffplay: `-fflags nobuffer`, a tiny probesize, or stdin |
| Player dies when the phone is rotated | picture size changed; the decoder must be restarted |
| Metallic, robotic audio | the decoded audio plane is padded — copying the whole plane feeds the padding to the device as samples |
| Crackling audio | jitter and loss: no reordering, or decoding on the receive thread |
| Audio silent, control packets arriving | a blocking read per socket in turn: a quiet socket throttles the busy one |
| A fresh receiver "starts fine" but logs nothing | a stale instance still owns the port (`SO_REUSEADDR` on Windows) |

The receiver's own log at `-v` shows the whole handshake, and `-vv` every
packet. Reading it beats guessing: every bug in this table was found in it, and
the ones that took longest were the ones where nothing in the log looked wrong.

---

## Measurements

Taken on this machine, with a real phone, because they are the kind of number
that is hard to find written down:

| Thing | Value |
| --- | --- |
| Video payload size, typical | 300 B – 19 kB per packet, ~30/s |
| Audio frame | 480 samples, ~11 ms, ~90 packets/s |
| Audio bitrate on the wire | ~64 kbit/s AAC-ELD |
| Decoded audio, PCM | 176 kB/s (44.1 kHz, stereo, s16) |
| PyAV audio plane padding | 128 bytes per 1024-sample frame — 32 samples of silence-shaped garbage if copied |
| Frame conversion to RGBA, 540x1170 | 0.44 ms |
| Phone screen | 1170x2532, portrait |
| `fp-setup` mode byte seen | 1 and 3 |

---

## Further reading

* [OpenAirplay protocol notes][openairplay] — the community reference
* [UxPlay][uxplay] — the most complete open mirroring receiver
* [RPiPlay][rpiplay] — where much of UxPlay's lineage comes from
* [shairport-sync][shairport] — audio only, and the best-documented of the lot
* [playfair][playfair] — the FairPlay key decryption
* ISO/IEC 14496-3 for `AudioSpecificConfig` and `ELDSpecificConfig`,
  ISO/IEC 14496-10 Annex E for the H.264 SPS

[playfair]: https://github.com/systemcrash/playfair
[rpiplay]: https://github.com/FD-/RPiPlay
[uxplay]: https://github.com/FDH2/UxPlay
[shairport]: https://github.com/mikebrady/shairport-sync
[openairplay]: https://openairplay.github.io/airplay-spec/
