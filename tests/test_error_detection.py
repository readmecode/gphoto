import importlib
import sys
from pathlib import Path

import pytest


@pytest.fixture
def main_module(monkeypatch, tmp_path):
    """Provide a freshly imported main module with isolated LOG_DIR."""
    module_name = "main"
    monkeypatch.setenv("LOG_DIR", str(tmp_path))
    project_root = Path(__file__).resolve().parents[1]
    sys.path.insert(0, str(project_root))
    if module_name in sys.modules:
        del sys.modules[module_name]
    module = importlib.import_module(module_name)
    yield module
    if module_name in sys.modules:
        del sys.modules[module_name]
    # Remove project_root we added to sys.path to keep environment clean
    if str(project_root) in sys.path:
        sys.path.remove(str(project_root))


def test_is_nonrecoverable_media_error_detects_google_message(main_module):
    message = "upload failed: Failed: There was an error while trying to create this media item. (3)"
    assert main_module.is_nonrecoverable_media_error(message) is True


def test_is_nonrecoverable_media_error_detects_preview_message(main_module):
    message = (
        "It may be damaged or use a file format that Preview doesn\u2019t recognize."
    )
    assert main_module.is_nonrecoverable_media_error(message) is True


def test_is_nonrecoverable_media_error_ignores_other_errors(main_module):
    message = "some transient network error"
    assert main_module.is_nonrecoverable_media_error(message) is False
