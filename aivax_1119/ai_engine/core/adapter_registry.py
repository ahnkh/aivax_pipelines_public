from __future__ import annotations

import importlib.util
import logging
import os
import sys
from typing import Any, Dict, Optional


ADAPTER_ALIASES = {
    "gpt": "chatgpt",
}


class _AdapterHandle:
    __slots__ = ("module", "mtime", "path")

    def __init__(self, module: Any, mtime: float, path: str) -> None:
        self.module = module
        self.mtime = mtime
        self.path = path


class AdapterRegistry:
    def __init__(self, adapters_dir: str, hot_reload: bool = True) -> None:
        self.adapters_dir = adapters_dir
        self.hot_reload = hot_reload
        self._cache: Dict[str, _AdapterHandle] = {}

    def get(self, service_id: str) -> Optional[Any]:
        sid = (service_id or "").lower()
        if not sid:
            return None
        adapter_id = ADAPTER_ALIASES.get(sid, sid)
        path = os.path.join(self.adapters_dir, f"{adapter_id}_engine.py")
        if not os.path.isfile(path):
            return None
        upload_path = os.path.join(self.adapters_dir, f"{adapter_id}_upload.py")
        try:
            mtime = os.path.getmtime(path)
            if os.path.isfile(upload_path):
                mtime = max(mtime, os.path.getmtime(upload_path))
        except OSError:
            return None

        handle = self._cache.get(sid)
        if handle and (not self.hot_reload or handle.mtime == mtime):
            return handle.module

        try:
            module = self._load_module(adapter_id, path, upload_path if os.path.isfile(upload_path) else None)
        except Exception:
            logging.exception("adapter load failed service=%s path=%s", sid, path)
            if handle:
                return handle.module
            return None

        self._cache[sid] = _AdapterHandle(module, mtime, path)
        return module

    def _load_module(self, sid: str, path: str, upload_path: Optional[str] = None) -> Any:
        adapters_parent = os.path.dirname(self.adapters_dir)
        if adapters_parent and adapters_parent not in sys.path:
            sys.path.insert(0, adapters_parent)

        # engine 이 `from adapters import {sid}_upload` 로 묶이므로 upload 를 먼저 재적재
        if upload_path:
            upload_name = f"adapters.{sid}_upload"
            upload_spec = importlib.util.spec_from_file_location(upload_name, upload_path)
            if upload_spec is not None and upload_spec.loader is not None:
                upload_mod = importlib.util.module_from_spec(upload_spec)
                sys.modules[upload_name] = upload_mod
                # `adapters` 패키지에서 attribute 로도 보이도록
                pkg = sys.modules.get("adapters")
                if pkg is not None:
                    setattr(pkg, f"{sid}_upload", upload_mod)
                upload_spec.loader.exec_module(upload_mod)

        name = f"ai_adapters.{sid}_engine"
        spec = importlib.util.spec_from_file_location(name, path)
        if spec is None or spec.loader is None:
            raise ImportError(f"cannot load adapter: {path}")
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        try:
            spec.loader.exec_module(module)
        except Exception:
            if name in sys.modules:
                del sys.modules[name]
            raise
        return module
