from setuptools import setup

APP = ['learning_review.py']
OPTIONS = {
    'argv_emulation': True,
    'iconfile': 'AppIcon.icns',
    'plist': {
        'CFBundleName': '日复一日',
        'CFBundleIdentifier': 'com.guohe.filerecall',
        'CFBundleLocalizations': ['en', 'zh-Hans'],
        'LSUIElement': True,  # 后台运行，无 Dock 图标
    },
    # 不显式列 PyObjC 包，避免 py2app 用 imp.find_module 报 No module named 'PyObjCTools'
    # 由 learning_review.py 的 import 自动带入
    'packages': [],
    'resources': ['en.lproj', 'zh-Hans.lproj'],
}

setup(
    app=APP,
    options={'py2app': OPTIONS},
    setup_requires=['py2app', 'pyobjc-framework-Cocoa'],
)