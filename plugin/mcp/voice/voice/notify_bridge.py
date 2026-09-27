"""Bridge from the voice MCP to the system notification cards.

Why: the cards live in their own package (notify/) — one card per parked
message, one "Ouvir mensagem" / "Listen" button — so the platform adapters can
evolve without touching the voice engine. The MCP locates that package
through SEBAS_NOTIFY_PATH: the directory that CONTAINS the notify/ package
(the MCP run.sh sets it per machine). Nothing is hardcoded here.

Nothing raises: a missing variable, a wrong path or a failing card all come
back as a payload and the caller falls back to the old flow (spoken short
notice + chat confirmation). The package is imported under a private module
name so it can never collide with an unrelated "notify" module.
"""
from __future__ import annotations

import importlib.util
import os
import sys

ENV = "SEBAS_NOTIFY_PATH"
_PACKAGE = "notify"
_MODULE = "voice_notify_cards"      # private alias; keeps sys.modules clean


def _find():
    """(module, root, problem): the notify package under $SEBAS_NOTIFY_PATH."""
    root = (os.environ.get(ENV) or "").strip()
    if not root:
        return None, root, f"{ENV} is not set"
    init_py = os.path.join(root, _PACKAGE, "__init__.py")
    if not os.path.isfile(init_py):
        return None, root, f"no {_PACKAGE}/__init__.py under {ENV}={root}"
    loaded = sys.modules.get(_MODULE)
    if loaded is not None and getattr(loaded, "__file__", None) == init_py:
        return loaded, root, None
    for name in [n for n in sys.modules
                 if n == _MODULE or n.startswith(_MODULE + ".")]:
        del sys.modules[name]             # previous root: drop stale submodules
    try:
        spec = importlib.util.spec_from_file_location(
            _MODULE, init_py,
            submodule_search_locations=[os.path.join(root, _PACKAGE)])
        if spec is None or spec.loader is None:
            return None, root, f"cannot load {_PACKAGE} from {ENV}={root}"
        module = importlib.util.module_from_spec(spec)
        sys.modules[_MODULE] = module
        spec.loader.exec_module(module)
    except Exception as e:
        sys.modules.pop(_MODULE, None)
        return None, root, f"import of {_PACKAGE} failed: {e!r}"
    if not callable(getattr(module, "show_card", None)):
        return None, root, f"{_PACKAGE} under {ENV} has no show_card()"
    return module, root, None


def resolve() -> dict:
    """Where the cards come from; status 'ok' | 'unavailable'."""
    module, root, problem = _find()
    if module is None:
        return {"status": "unavailable", "problem": problem,
                "next_step": (f"Set {ENV} to the directory that contains the "
                              f"{_PACKAGE}/ package to enable notification cards; "
                              "without it the spoken-notice flow is used.")}
    return {"status": "ok", "path": root}


def available() -> bool:
    """True when a notification card can be attempted."""
    return _find()[0] is not None


def show_card(text: str, *,
              butler: str = "Sebas",
              language: str = "en-us",
              context: str | None = None,
              urgency: str = "critical",
              play: bool = True) -> dict:
    """Shows the card through the notify package. Mirrors its contract and
    never raises: failures are payloads the caller can fall back from."""
    module, _root, problem = _find()
    if module is None:
        return {"status": "unavailable", "problem": problem,
                "next_step": ("Fall back to the spoken short notice and the chat "
                              "confirmation.")}
    try:
        result = module.show_card(text, butler=butler, language=language,
                                  context=context, urgency=urgency, play=play)
    except Exception as e:
        return {"status": "error", "problem": repr(e),
                "next_step": ("The card could not be shown; fall back to the spoken "
                              "short notice and the chat confirmation.")}
    return result or {"status": "error", "problem": "card backend returned nothing",
                      "next_step": ("The card could not be shown; fall back to the "
                                    "spoken short notice and the chat confirmation.")}
