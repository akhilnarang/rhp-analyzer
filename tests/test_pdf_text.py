from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import patch

from rhp_analyzer.pdf_text import PDF_PARSE_TIMEOUT_SECONDS, extract_pages


class PdfTextTests(TestCase):
    def test_parser_uses_the_bubblewrap_sandbox(self) -> None:
        with TemporaryDirectory() as directory:
            pdf_path = Path(directory) / "input.pdf"
            pdf_path.write_bytes(b"%PDF-1.4\n%%EOF")
            captured: dict[str, object] = {}

            def fake_run(command: list[str], **kwargs: object) -> None:
                captured["command"] = command
                captured.update(kwargs)
                kwargs["stdout"].write(b"page one\fpage two\f")

            with patch("rhp_analyzer.pdf_text.subprocess.run", fake_run):
                pages = extract_pages(pdf_path)

        command = captured["command"]
        self.assertEqual(command[0], "/usr/bin/bwrap")
        self.assertIn("--unshare-all", command)
        self.assertIn("--clearenv", command)
        self.assertIn(str(pdf_path), command)
        self.assertIn("/input.pdf", command)
        self.assertEqual(pages, ["page one", "page two"])
        self.assertEqual(captured["timeout"], PDF_PARSE_TIMEOUT_SECONDS)
        self.assertTrue(captured["start_new_session"])
