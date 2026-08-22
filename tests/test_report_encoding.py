"""HTML reports must be written as UTF-8 regardless of the platform default.

The report template declares <meta charset="utf-8"> and the card markup contains
characters like "≈" and "→". Path.write_text() with no encoding= uses the
platform default, which on a Russian/Windows console is cp1251 — there the write
died with UnicodeEncodeError and left a 0-byte report behind.
"""
from __future__ import annotations

import locale

from cardiag.inspect import report


def _force_cp1251(monkeypatch):
    """Make the platform default encoding cp1251, as on a ru-RU Windows box."""
    monkeypatch.setattr(locale, "getpreferredencoding", lambda *a: "cp1251")
    if hasattr(locale, "getencoding"):
        monkeypatch.setattr(locale, "getencoding", lambda *a: "cp1251")


def test_report_survives_cp1251_default(tone_wav, tmp_path, monkeypatch):
    """report() writes readable UTF-8 even when the locale can't encode "≈"."""
    _force_cp1251(monkeypatch)
    out = tmp_path / "report.html"

    report([tone_wav], out_path=str(out), with_clap=False)

    assert out.stat().st_size > 0, "report was created but left empty"
    html = out.read_text(encoding="utf-8")
    assert "≈" in html, "non-ASCII card text did not survive the write"
    assert 'charset="utf-8"' in html


def test_report_bytes_are_utf8(tone_wav, tmp_path):
    """The bytes on disk decode as UTF-8, matching the declared charset."""
    out = tmp_path / "report.html"
    report([tone_wav], out_path=str(out), with_clap=False)

    out.read_bytes().decode("utf-8")  # raises UnicodeDecodeError if mis-encoded
