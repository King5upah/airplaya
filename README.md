# airplaya

An iPhone and iPad screen mirroring receiver for Windows, over Wi-Fi **or the
USB cable**, with no app on the phone. Over Wi-Fi the receiver shows up in the
normal iOS **Screen Mirroring** list; over the cable it just starts.

The receiver is Python. A Flutter desktop app is available as a front end, and
without it the video is displayed by `ffplay`.

```
Wi-Fi:
iPhone ──mDNS──> airplaya (:7000 RTSP)
       ──pair-setup / pair-verify / fp-setup──> keys
       ──H.264 over TCP (:7100, AES-CTR)──────> decrypt ─> window

Cable:
iPhone ──USB bulk endpoints (subclass 0x2A)──> H.264 in the clear ─> window
```

## Status

Working over Wi-Fi: discovery, pairing, the FairPlay handshake, video
decryption, H.264/H.265 display, audio playback on an output device of your
choosing, and clip recording.

Working over the cable: the QuickTime capture protocol — full frame rate,
48 kHz audio, no encryption, lower latency. Needs a USB driver prerequisite on
Windows; see [docs/wired.md](docs/wired.md).

Not yet: ALAC audio. Wi-Fi mirroring uses AAC-ELD, which is what is
implemented; a client that negotiates ALAC gets video only. See
[Audio](#audio).

## Requirements

* Windows 10 or 11
* Python 3.10+
* [ffmpeg](https://ffmpeg.org/) on `PATH`, for `ffplay`
* A C compiler, once, to build the FairPlay helper — [zig](https://ziglang.org/)
  is the smallest option (`winget install zig.zig`) and needs no admin rights
* For cable mirroring only: a USB driver that can select a non-first
  configuration. Windows' own cannot — [docs/wired.md](docs/wired.md#7-windows-the-driver-problem)

## Install

```powershell
git clone https://github.com/King5upah/airplaya.git
cd airplaya
pip install -e .
python scripts/build_playfair.py
```

## Run

```powershell
python -m airplaya
```

Then on the iPhone: **Control Centre → Screen Mirroring → airplaya**.

Over the cable instead:

```powershell
python -m airplaya --list-usb-devices
python -m airplaya --source usb
```

Useful flags:

| Flag | What it does |
| --- | --- |
| `--source usb` | mirror over the cable instead of Wi-Fi |
| `--usb-serial UDID` | which device, when more than one is plugged in |
| `--list-usb-devices` | print the iOS devices on USB and exit |
| `--name NAME` | the name shown in the iOS list |
| `--sink file -o out.h264` | write the raw stream instead of playing it |
| `--sink app --video-port N` | decode here and stream RGBA frames to a client on that port |
| `--sink null` | discard the video (protocol testing) |
| `--resolution 2532x1170` | resolution to advertise |
| `--list-audio-devices` | print the output devices and exit |
| `--audio-device 28` | play on that device (an index, or part of its name) |
| `--no-audio` | video only |
| `-v` / `-vv` | protocol log / per-packet trace |

### The desktop app

A Flutter front end lives in its own repository. It runs the receiver, picks the
audio output device, repairs the firewall rules, and draws the mirrored picture
in its own window using `--sink app`.

Everything the receiver does is available from the command line; the app is a
convenience, not a requirement.

## Firewall

The iPhone opens a TCP connection *to* this machine, so Windows Firewall has to
allow it. Without a rule the receiver appears in the iOS list and then fails
with "unable to connect". The desktop app can add the rules for you, or:

```powershell
netsh advfirewall firewall add rule name="airplaya" dir=in action=allow ^
  protocol=TCP localport=7000,7100 profile=any enable=yes
netsh advfirewall firewall add rule name="airplaya udp" dir=in action=allow ^
  protocol=UDP localport=6000,6001,7010 profile=any enable=yes
```

Ports used: TCP 7000 (control), TCP 7100 (video), UDP 6000/6001 (audio),
UDP 7010 (clock), UDP 5353 (mDNS).

## How it works

For the protocol itself — discovery, the control channel, the key derivation
chain, the stream formats, and what this receiver does differently from a real
Apple TV — see **[docs/airplay.md](docs/airplay.md)**. It is written from what
went over the wire here, including the bugs that were most expensive to find.

The cable is a completely different protocol, and has its own write-up:
**[docs/wired.md](docs/wired.md)** — the hidden USB configuration, the clock
handshake, the sample buffers, and why Windows makes it hard.

### The code

| Module | Responsibility |
| --- | --- |
| `discovery.py` | Bonjour advertisement for `_airplay._tcp` and `_raop._tcp` |
| `rtsp/` | the control channel: request parsing and handlers |
| `crypto/pairing.py` | legacy pairing; produces the shared secret |
| `crypto/fairplay.py` | the FairPlay `fp-setup` exchange |
| `crypto/playfair.py` | binding to the native white-box key decryption |
| `crypto/keys.py` | key derivation for the media streams |
| `stream/mirror.py` | the video stream: framing, AES-CTR, NAL conversion |
| `stream/nal.py` | length-prefixed NAL units to Annex-B |
| `stream/audio.py` | the audio stream: RTP, AES-CBC, frame extraction |
| `stream/asc.py` | rebuilding the AAC configuration the decoder needs |
| `sink/` | where the media goes: ffplay, a client app, a file, or nowhere |
| `wired/usb.py` | the hidden USB configuration and the bulk endpoints |
| `wired/coremedia.py` | CoreMedia serialisation: dicts, times, sample buffers |
| `wired/packets.py` | PING / SYNC / RPLY / ASYN, and the replies we send |
| `wired/session.py` | the clock handshake, and the media once it flows |

Two details cause most of the trouble when writing one of these:

* **`GET /info` with a `txtAirPlay` qualifier.** The client's first request asks
  for the TXT record over the control channel. Answering with an empty plist
  makes iOS abandon the session in milliseconds, which looks exactly like a
  network fault.
* **Keystream alignment.** One AES-CTR keystream spans the whole video stream,
  but each payload starts on a block boundary. A payload's trailing partial
  block consumes a full block of keystream, and the leftover bytes decrypt the
  start of the next payload.

## Audio

Audio arrives as RTP on UDP 6000, encrypted with AES-128-CBC — re-keyed from
the session IV for every packet, and covering only the whole blocks.

Inside is AAC-ELD at 44.1 kHz, 480 samples per frame. Bare access units: the
`AudioSpecificConfig` a decoder needs is never transmitted, because both ends
know the format from SETUP. So `stream/asc.py` reconstructs it from the `ct` and
`spf` values in the SETUP request, and it is handed to the decoder as extradata.

That last requirement rules out driving `ffmpeg` as a subprocess — its command
line has no way to supply extradata, and ADTS cannot describe ELD. Hence PyAV
for decoding, and PortAudio (via `sounddevice`) for playback, which unlike
ffplay can be pointed at a specific output device:

```powershell
python -m airplaya --list-audio-devices
python -m airplaya --audio-device "HyperX"
```

ALAC (`ct = 2`) is recognised and skipped. It needs the codec's magic cookie
from a format packet this receiver does not parse yet.

## Tests

```powershell
python -m pytest
```

The suite covers the keystream alignment, the key derivation, the NAL
conversion, the RTSP message layer, and the whole cable protocol against packets
captured from a real phone — everything that can be checked without an iPhone in
the room.

## Licence and credits

GPL-3.0-or-later. See [LICENSE](LICENSE).

The cable protocol was reverse engineered by Daniel Paulus in
[quicktime_video_hack][qvh] (MIT); the captured packets in `tests/data/wired`
come from that project and are what the parsers are tested against.

`native/playfair/` is vendored C from the [playfair][playfair] project, by way
of [RPiPlay][rpiplay] and [UxPlay][uxplay]. The FairPlay `fp-setup` reply
constants come from UxPlay's `fairplay_playfair.c` (LGPL-2.1). The protocol
itself was worked out by those projects and by [OpenAirplay][openairplay]; this
implementation follows their findings.

[qvh]: https://github.com/danielpaulus/quicktime_video_hack
[playfair]: https://github.com/systemcrash/playfair
[rpiplay]: https://github.com/FD-/RPiPlay
[uxplay]: https://github.com/FDH2/UxPlay
[openairplay]: https://openairplay.github.io/airplay-spec/
