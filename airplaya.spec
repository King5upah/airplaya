# -*- mode: python ; coding: utf-8 -*-


a = Analysis(
    ['C:\\Users\\Supah\\dev\\airplaya\\src\\airplaya\\__main__.py'],
    pathex=['C:\\Users\\Supah\\dev\\airplaya\\src'],
    binaries=[('C:\\Users\\Supah\\dev\\airplaya\\src\\airplaya\\crypto\\playfair.dll', 'airplaya/crypto'), ('C:\\Users\\Supah\\AppData\\Roaming\\Python\\Python314\\site-packages\\libusb_package\\libusb-1.0.dll', 'libusb_package')],
    datas=[],
    hiddenimports=['airplaya.wired', 'airplaya.wired.receiver', 'airplaya.wired.usb', 'usb.backend.libusb1', 'sounddevice', '_sounddevice_data', 'zeroconf', 'zeroconf._utils.ipaddress', 'zeroconf._handlers.answers'],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='airplaya',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name='airplaya',
)
