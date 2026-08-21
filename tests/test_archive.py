import hashlib
import zipfile
from pathlib import Path
from tempfile import NamedTemporaryFile, TemporaryDirectory
from unittest import TestCase
from unittest.mock import patch

from rhp_analyzer.archive import (
    ArchiveError,
    ArchiveSelectionError,
    ExtractedPdf,
    _extract_member,
    classify_prospectus,
    extract_prospectus_from_zip,
)


class ArchiveTests(TestCase):
    @staticmethod
    def fake_extract(
        _archive_path: Path,
        info: zipfile.ZipInfo,
        *,
        max_pdf_bytes: int,
    ) -> ExtractedPdf:
        del max_pdf_bytes
        data = b"%PDF-" + info.filename.encode()
        with NamedTemporaryFile(suffix=".pdf", delete=False) as output:
            output.write(data)
            path = Path(output.name)
        return ExtractedPdf(
            path=path,
            filename=Path(info.filename).name,
            size=len(data),
            sha256=hashlib.sha256(data).hexdigest(),
        )

    def test_checks_every_pdf_and_uses_content_not_the_name(self) -> None:
        with TemporaryDirectory() as directory:
            archive_path = Path(directory) / "offer.zip"
            with zipfile.ZipFile(archive_path, "w") as archive:
                archive.writestr("RHP_decoy.pdf", b"decoy")
                archive.writestr("documents/actual_offer.pdf", b"offer")
            checked: list[str] = []

            def classify(path: Path) -> tuple[str, int] | None:
                member = path.read_bytes().removeprefix(b"%PDF-").decode()
                checked.append(member)
                if member.endswith("actual_offer.pdf"):
                    return "RHP", 3
                return None

            with (
                patch(
                    "rhp_analyzer.archive._extract_member",
                    side_effect=self.fake_extract,
                ),
                patch(
                    "rhp_analyzer.archive.classify_prospectus",
                    side_effect=classify,
                ),
            ):
                result = extract_prospectus_from_zip(
                    archive_path,
                    max_pdf_bytes=1_000_000,
                )

            self.assertEqual(
                checked,
                ["RHP_decoy.pdf", "documents/actual_offer.pdf"],
            )
            self.assertEqual(result.filename, "actual_offer.pdf")
            result.path.unlink()

    def test_classifies_a_formal_rhp_title_near_the_document_start(self) -> None:
        pages = [
            "RED HERRING PROSPECTUS Dated August 10, 2026 "
            + "Scan this code to view the RHP and Abridged Prospectus"
        ]
        with patch("rhp_analyzer.archive.extract_pdf_sample", return_value=pages):
            classification = classify_prospectus(Path("untrusted-name.pdf"))
        self.assertEqual(classification, ("RHP", 3))

    def test_rejects_an_incidental_rhp_reference(self) -> None:
        pages = [
            "COMPANY APPLICATION FORM "
            + ("company details " * 100)
            + "See this Red Herring Prospectus for more information."
        ]
        with patch("rhp_analyzer.archive.extract_pdf_sample", return_value=pages):
            classification = classify_prospectus(Path("RHP_decoy.pdf"))
        self.assertIsNone(classification)

    def test_rejects_an_unsafe_path_before_extraction(self) -> None:
        with TemporaryDirectory() as directory:
            archive_path = Path(directory) / "unsafe.zip"
            with zipfile.ZipFile(archive_path, "w") as archive:
                archive.writestr("../RHP_offer.pdf", b"unsafe")
            with (
                patch("rhp_analyzer.archive._extract_member") as extract,
                self.assertRaisesRegex(ArchiveError, "unsafe member path"),
            ):
                extract_prospectus_from_zip(
                    archive_path,
                    max_pdf_bytes=1_000_000,
                )
            extract.assert_not_called()

    def test_rejects_several_equal_offer_documents(self) -> None:
        with TemporaryDirectory() as directory:
            archive_path = Path(directory) / "ambiguous.zip"
            with zipfile.ZipFile(archive_path, "w") as archive:
                archive.writestr("one.pdf", b"one")
                archive.writestr("nested/two.pdf", b"two")
            with (
                patch(
                    "rhp_analyzer.archive._extract_member",
                    side_effect=self.fake_extract,
                ),
                patch(
                    "rhp_analyzer.archive.classify_prospectus",
                    return_value=("RHP", 3),
                ),
                self.assertRaisesRegex(
                    ArchiveSelectionError,
                    "several equally valid",
                ),
            ):
                extract_prospectus_from_zip(
                    archive_path,
                    max_pdf_bytes=1_000_000,
                )

    def test_member_extraction_uses_bubblewrap(self) -> None:
        with TemporaryDirectory() as directory:
            archive_path = Path(directory) / "offer.zip"
            archive_path.write_bytes(b"PK")
            info = zipfile.ZipInfo("folder/offer.pdf")
            data = b"%PDF-1.4\n%%EOF"
            info.file_size = len(data)
            captured: dict[str, object] = {}

            def fake_run(command: list[str], **kwargs: object) -> None:
                captured["command"] = command
                captured.update(kwargs)
                kwargs["stdout"].write(data)

            with patch("rhp_analyzer.archive.subprocess.run", fake_run):
                result = _extract_member(
                    archive_path,
                    info,
                    max_pdf_bytes=1_000_000,
                )

            command = captured["command"]
            self.assertEqual(command[0], "/usr/bin/bwrap")
            self.assertIn("--unshare-all", command)
            self.assertIn("--clearenv", command)
            self.assertIn("/usr/bin/python3", command)
            self.assertIn(info.filename, command)
            self.assertEqual(
                captured["timeout"],
                60,
            )
            self.assertTrue(captured["start_new_session"])
            self.assertEqual(result.sha256, hashlib.sha256(data).hexdigest())
            result.path.unlink()
