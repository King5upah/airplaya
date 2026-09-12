# Captured QuickTime-over-USB packets

Real frames from an iPhone's AV endpoints, used to test the parsers and the
replies without a phone in the room. Each file holds one frame **including** its
4-byte length prefix, so a test that feeds our parsers has to skip the first
four bytes — that is the framing the reader strips.

These fixtures come from
[`quicktime_video_hack`](https://github.com/danielpaulus/quicktime_video_hack)
by Daniel Paulus, MIT licensed:

```
Copyright (c) 2019 danielpaulus

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
```

The `asyn-hpd1` and `asyn-hpa1` files are packets that tool *sent*, which makes
them the useful check: our serialiser has to produce the same bytes, down to
the device name it used (`Valeria`) and the 1920x1200 screen it claimed.
