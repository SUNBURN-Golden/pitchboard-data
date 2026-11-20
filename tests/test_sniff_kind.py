import unittest
from pbdata.collect.executor import kind_for, sniff_kind


class SniffKindTests(unittest.TestCase):
    def test_octet_stream_pdf_is_pdf(self):
        k = kind_for("https://x.kr/fileDownloadWeb.do?filIdx=1", "application/octet-stream")
        self.assertEqual(k, "html")
        self.assertEqual(sniff_kind(k, b"%PDF-1.6\r%\xe2\xe3"), "pdf")

    def test_octet_stream_png_jpeg_webp_are_images(self):
        for body in (b"\x89PNG\r\n\x1a\n\x00", b"\xff\xd8\xff\xe0", b"RIFF\x00\x00\x00\x00WEBPVP8 "):
            self.assertEqual(sniff_kind("html", body), "image")

    def test_real_html_and_declared_kinds_unchanged(self):
        self.assertEqual(sniff_kind("html", b"<html><body>%PDF- in text"), "html")
        self.assertEqual(sniff_kind("feed", b"%PDF-"), "feed")
        self.assertEqual(sniff_kind("html", b""), "html")
