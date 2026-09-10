# airplaya

An AirPlay mirroring receiver for Windows. Put your iPhone or iPad screen on
your desktop over Wi-Fi, with no app on the phone: the receiver shows up in the
normal iOS **Screen Mirroring** list.

The receiver is Python. A small Flutter desktop app is included as a front end,
and the video is displayed by `ffplay`.

```
iPhone ──mDNS──> airplaya (:7000 RTSP)
       ──pair-setup / pair-verify / fp-setup──> keys
       ──H.264 over TCP (:7100, AES-CTR)──────> decrypt ─> ffplay
```

## Status

Working: discovery, pairing, the FairPlay handshake, video decryption, and
H.264/H.265 display.

Not yet: audio playback. The audio ports are bound and drained so that iOS is
happy, but the packets are discarded. See [Audio](#audio).

## Requirements

* Windows 10 or 11
* Python 3.10+
* [ffmpeg](https://ffmpeg.org/) on `PATH`, for `ffplay`
* A C compiler, once, to build the FairPlay helper — [zig](https://ziglang.org/)
  is the smallest option (`winget install zig.zig`) and needs no admin rights

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

Useful flags:

| Flag | What it does |
| --- | --- |
| `--name NAME` | the name shown in the iOS list |
| `--sink file -o out.h264` | write the raw stream instead of playing it |
| `--sink null` | discard the video (protocol testing) |
| `--resolution 2532x1170` | resolution to advertise |
| `-v` / `-vv` | protocol log / per-packet trace |

### The desktop app

```powershell
cd gui
flutter build windows --release
```

The app starts and stops the receiver, shows its log, and offers a one-click
repair for the firewall rules described below.

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
| `sink/` | where the video goes: ffplay, a file, or nowhere |

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

Mirroring negotiates an audio stream alongside the video one. The receiver binds
and drains those ports, but does not decode them yet: the payloads are AES-CBC
encrypted AAC-ELD or ALAC frames, and both need their codec configuration
carried out-of-band before a decoder will touch them.

## Tests

```powershell
python -m pytest
```

The suite covers the keystream alignment, the key derivation, the NAL
conversion, and the RTSP message layer — everything that can be checked without
an iPhone in the room.

## Licence and credits

GPL-3.0-or-later. See [LICENSE](LICENSE).

`native/playfair/` is vendored C from the [playfair][playfair] project, by way
of [RPiPlay][rpiplay] and [UxPlay][uxplay]. The FairPlay `fp-setup` reply
constants come from UxPlay's `fairplay_playfair.c` (LGPL-2.1). The protocol
itself was worked out by those projects and by [OpenAirplay][openairplay]; this
implementation follows their findings.

[playfair]: https://github.com/systemcrash/playfair
[rpiplay]: https://github.com/FD-/RPiPlay
[uxplay]: https://github.com/FDH2/UxPlay
[openairplay]: https://openairplay.github.io/airplay-spec/
