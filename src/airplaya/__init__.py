"""airplaya — an AirPlay mirroring receiver for Windows.

The public surface is deliberately small: build a `Config`, hand it to
`Receiver`, and call `serve_forever()`. Everything else is an implementation
detail of the AirPlay v1 mirroring protocol.
"""

from airplaya.config import Config
from airplaya.receiver import Receiver

__version__ = "0.1.0"
__all__ = ["Config", "Receiver", "__version__"]
