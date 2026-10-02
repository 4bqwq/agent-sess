from importlib.metadata import version

from sess import __version__


def test_public_version_matches_package_metadata():
    assert __version__ == version("sess-cli")
