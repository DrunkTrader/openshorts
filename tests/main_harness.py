"""Import ``main`` where the heavy ML stack is not installed (CI's light set).

main.py imports torch/ultralytics/mediapipe/scenedetect at the top. Tests of
its download code need none of them, so the missing ones are replaced by
MagicMock modules for the life of a ``with`` block, and everything imported
meanwhile is dropped again so no other test sees a stubbed ``main``.
"""
import contextlib
import importlib
import importlib.machinery
import importlib.util
import os
import sys
import types
from unittest import mock

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_HEAVY = ("torch", "ultralytics", "mediapipe", "scenedetect", "tqdm", "faster_whisper",
          "onnx_asr", "onnxruntime", "cv2", "numpy", "google", "google.genai",
          "google.genai.types", "dotenv", "yt_dlp", "yt_dlp.utils", "yt_dlp.version")


def _installed(name):
    try:
        return importlib.util.find_spec(name) is not None
    except (ImportError, ValueError):
        return False


class _StubModule(types.ModuleType):
    __path__ = []

    def __getattr__(self, name):
        if name == "__version__":
            return "0"
        if name.startswith("__"):
            raise AttributeError(name)
        if name == "version":
            value = _StubModule(f"{self.__name__}.version")
            setattr(self, name, value)
            return value
        value = mock.MagicMock(name=f"{self.__name__}.{name}")
        setattr(self, name, value)
        return value


class _StubFinder:
    """Serves ``pkg`` and every ``pkg.sub.module`` of a stubbed package."""

    def __init__(self, roots):
        self.roots = roots

    def find_spec(self, name, path=None, target=None):
        if name.split(".")[0] not in self.roots:
            return None
        return importlib.machinery.ModuleSpec(name, self, is_package=True)

    def create_module(self, spec):
        return _StubModule(spec.name)

    def exec_module(self, module):
        pass


@contextlib.contextmanager
def light_main():
    before = set(sys.modules)
    roots = {n for n in _HEAVY if "." not in n and n not in sys.modules and not _installed(n)}
    finder = _StubFinder(roots)
    sys.meta_path.insert(0, finder)
    try:
        yield importlib.import_module("main")
    finally:
        sys.meta_path.remove(finder)
        for name in set(sys.modules) - before:
            mod = sys.modules[name]
            path = str(getattr(mod, "__file__", "") or "")
            if name.split('.')[0] in roots or (path.startswith(_ROOT) and "site-packages" not in path):
                del sys.modules[name]
