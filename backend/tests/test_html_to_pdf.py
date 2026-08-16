import sys
import types
import pytest
from unittest.mock import MagicMock, patch
from pathlib import Path
from app.services.document_conversion.html_to_pdf import convert_html_to_pdf


@pytest.fixture
def fake_weasyprint(monkeypatch):
    """Inject a stub weasyprint module so tests run without system libpango.

    The old ``@patch("weasyprint.HTML")`` decorator imported the real
    weasyprint at collection time, which fails on machines without pango
    (OSError: cannot load library 'libpango-1.0-0'). The production code
    imports weasyprint lazily inside convert_html_to_pdf, so a fake module
    in sys.modules is enough to exercise the fallback branch.
    """
    fake = types.ModuleType("weasyprint")
    monkeypatch.setitem(sys.modules, "weasyprint", fake)
    return fake


@pytest.mark.asyncio
@patch("app.services.document_conversion.html_to_pdf.chrome_executable")
@patch("subprocess.Popen")
@patch("time.time")
async def test_convert_html_to_pdf_linux(mock_time, mock_popen, mock_chrome_exec, fake_weasyprint):
    mock_chrome_exec.return_value = "/usr/bin/google-chrome"
    mock_time.side_effect = [1000.0, 1010.0]  # Fails deadline immediately

    # Mock subprocess.Popen
    mock_proc = MagicMock()
    mock_popen.return_value = mock_proc

    # Mock weasyprint HTML write_pdf
    mock_weasy_instance = MagicMock()
    fake_weasyprint.HTML = MagicMock(return_value=mock_weasy_instance)

    src = Path("/tmp/src.html")
    tgt = Path("/tmp/tgt.pdf")

    with patch("sys.platform", "linux"):
        res = await convert_html_to_pdf(src, tgt, "tgt.pdf", {})

    assert mock_popen.called
    args = mock_popen.call_args[0][0]
    assert "--no-sandbox" in args
    assert "--disable-setuid-sandbox" in args
    assert "WeasyPrint" in res


@pytest.mark.asyncio
@patch("app.services.document_conversion.html_to_pdf.chrome_executable")
@patch("subprocess.Popen")
@patch("time.time")
async def test_convert_html_to_pdf_darwin(mock_time, mock_popen, mock_chrome_exec, fake_weasyprint):
    mock_chrome_exec.return_value = "/usr/bin/google-chrome"
    mock_time.side_effect = [1000.0, 1010.0]  # Fails deadline immediately

    # Mock subprocess.Popen
    mock_proc = MagicMock()
    mock_popen.return_value = mock_proc

    # Mock weasyprint HTML write_pdf
    mock_weasy_instance = MagicMock()
    fake_weasyprint.HTML = MagicMock(return_value=mock_weasy_instance)

    src = Path("/tmp/src.html")
    tgt = Path("/tmp/tgt.pdf")

    with patch("sys.platform", "darwin"):
        res = await convert_html_to_pdf(src, tgt, "tgt.pdf", {})

    assert mock_popen.called
    args = mock_popen.call_args[0][0]
    assert "--no-sandbox" not in args
    assert "--disable-setuid-sandbox" not in args
    assert "WeasyPrint" in res
