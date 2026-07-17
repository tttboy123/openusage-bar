from setuptools import setup

from build_support import apply_py2app_static_zlib_patch
from openusage_bar.bundle_config import APP_NAME, APP_VERSION, info_plist


apply_py2app_static_zlib_patch()

setup(
    name=APP_NAME,
    version=APP_VERSION,
    app=["openusage_settings.py"],
    packages=["openusage_bar"],
    package_data={"openusage_bar": ["resources/*.json"]},
    options={
        "py2app": {
            "plist": info_plist(),
            "packages": ["openusage_bar"],
            "arch": "arm64",
        }
    },
    setup_requires=["py2app==0.28.10"],
)
