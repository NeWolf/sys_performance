# -*- mode: python ; coding: utf-8 -*-


a = Analysis(
    ['/Users/ext.anle6/PycharmProjects/sys_performance/main.py'],
    pathex=[],
    binaries=[],
    datas=[('/Users/ext.anle6/PycharmProjects/sys_performance/frontend/dist', 'frontend/dist'), ('/Users/ext.anle6/PycharmProjects/sys_performance/frontend/src/sysmonitor_test/sysmonitor', 'frontend/src/sysmonitor_test')],
    hiddenimports=[],
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
    name='SysMonitor',
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
    name='SysMonitor',
)
