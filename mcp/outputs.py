"""nbformat output formatting — pure, zero async, no I/O.

`_format_outputs` turns a list of nbformat outputs into (structured, combined_text)
for the MCP result payload; `_strip_ansi` drops terminal color codes from
tracebacks. Both are used across execution + notebook-read paths.
"""
from __future__ import annotations

import re
from typing import Any

from config import INCLUDE_IMAGE_BYTES, MAX_OUTPUT_CHARS

_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")


def _strip_ansi(s: str) -> str:
    return _ANSI_RE.sub("", s)


def _format_outputs(outputs: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], str]:
    """nbformat outputs -> (structured, combined_text)."""
    structured: list[dict[str, Any]] = []
    text_parts: list[str] = []
    for o in outputs:
        ot = o.get("output_type")
        if ot == "stream":
            t = o.get("text", "")
            structured.append({"type": "stream", "name": o.get("name"), "text": t})
            text_parts.append(t)
        elif ot in ("execute_result", "display_data"):
            data = o.get("data", {})
            plain = data.get("text/plain", "")
            item: dict[str, Any] = {"type": ot, "text": plain}
            imgs = [m for m in data if m.startswith("image/")]
            if imgs:
                item["images"] = []
                for m in imgs:
                    b = data[m]
                    item["images"].append(
                        {"mime": m, "bytes_b64": b} if INCLUDE_IMAGE_BYTES else {"mime": m, "size_b64": len(b)}
                    )
                    text_parts.append(f"[{m} {len(data[m])} b64 chars]")
            structured.append(item)
            if plain:
                text_parts.append(plain)
        elif ot == "error":
            tb = _strip_ansi("\n".join(o.get("traceback", [])))
            structured.append(
                {"type": "error", "ename": o.get("ename"), "evalue": o.get("evalue"), "traceback": tb}
            )
            text_parts.append(f"{o.get('ename')}: {o.get('evalue')}\n{tb}")
    text = "\n".join(p for p in text_parts if p)
    if len(text) > MAX_OUTPUT_CHARS:
        text = text[:MAX_OUTPUT_CHARS] + f"\n... [truncated, {len(text)} chars total]"
    return structured, text
