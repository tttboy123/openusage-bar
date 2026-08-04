import sys

from setuptools import setup


common = {
    "name": "openusage-bar",
    "version": "0.6.0",
    "description": (
        "UsageHub (formerly OpenUsage Bar): local-first AI usage ledger "
        "and observation component for schedulers and native clients."
    ),
    "packages": ["openusage_bar"],
    "package_data": {"openusage_bar": ["resources/*.json"]},
    "python_requires": ">=3.11",
    "entry_points": {
        "console_scripts": [
            "openusage-bar = openusage_bar.collector_cli:main",
        ],
    },
}

if sys.platform == "darwin":
    from build_support import apply_py2app_static_zlib_patch
    from openusage_bar.bundle_config import APP_NAME, APP_VERSION, info_plist

    apply_py2app_static_zlib_patch()
    common.update(
        dict(
            # The build script expects the py2app artifact and executable to be
            # named "OpenUsage Provider Settings" so the Collector launcher can
            # exec it. The user-visible display name stays "UsageHub Provider
            # Settings" through info_plist().
            name="OpenUsage Provider Settings",
            version=APP_VERSION,
            app=["openusage_settings.py"],
            options={
                "py2app": {
                    "plist": info_plist(),
                    "packages": ["openusage_bar"],
                    "excludes": ["test", "tests", "unittest"],
                    "arch": "arm64",
                }
            },
            setup_requires=["py2app==0.28.10"],
        )
    )

setup(**common)
