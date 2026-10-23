"""Slack file uploads -> per-user docs. Offline: fake downloader, PDF built in the test."""

import importlib.util
import os
import sys
import tempfile
import unittest
import urllib.error
import urllib.request
from pathlib import Path

_tmp = tempfile.mkdtemp()
os.environ.update(
    {
        "DB_PATH": os.path.join(_tmp, "test.db"),
        "CALL_LOG_PATH": os.path.join(_tmp, "calls.jsonl"),
        "GATE_MODEL": "",  # offline: the salience gate (on by default) never hits the network
        "OPENROUTER_API_KEY": "test-not-a-key",
    }
)
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from chatbot import config, db, uploads  # noqa: E402
from chatbot.capabilities import docs  # noqa: E402
from chatbot.memory import doc_notes  # noqa: E402

HAS_PYPDF = importlib.util.find_spec("pypdf") is not None
URL = "https://files.slack.com/files-pri/T1-F1/download/x"


def tiny_pdf(text: str) -> bytes:
    """One-page PDF with `text` in Helvetica, xref offsets computed so parsers need no repair."""
    stream = f"BT /F1 12 Tf 72 720 Td ({text}) Tj ET".encode()
    objs = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R"
        b" /Resources << /Font << /F1 5 0 R >> >> >>",
        b"<< /Length %d >>\nstream\n" % len(stream) + stream + b"\nendstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    out, offsets = b"%PDF-1.4\n", []
    for i, body in enumerate(objs, 1):
        offsets.append(len(out))
        out += b"%d 0 obj\n" % i + body + b"\nendobj\n"
    xref = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objs) + 1)
    out += b"".join(b"%010d 00000 n \n" % o for o in offsets)
    out += b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (len(objs) + 1, xref)
    return out


class UploadsTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self._orig = (docs.UPLOADS_DIR, uploads.download, config.MAX_FILE_BYTES)
        docs.UPLOADS_DIR = Path(self.dir)
        config.MAX_FILE_BYTES = 0
        self.fetched, self.data = [], b""
        uploads.download = self._fake_download

    def tearDown(self):
        docs.UPLOADS_DIR, uploads.download, config.MAX_FILE_BYTES = self._orig

    def _fake_download(self, url, token, max_bytes=0):
        self.fetched.append((url, token, max_bytes))
        return self.data

    def _file(self, name, size=None, **kw):
        f = {"id": "F1", "name": name, "url_private_download": URL, "size": size or len(self.data)}
        f.update(kw)
        return f

    def test_text_types_become_readable_docs_for_owner_only(self):
        for name, body in (
            ("notes.txt", "alpha line\n\nbeta line"),
            ("readme.md", "# Title\n\nkiwi fact"),
            ("table.csv", "a,b\n1,2"),
            ("data.json", '{"k": "v"}'),
        ):
            with self.subTest(name):
                self.data = body.encode()
                doc_id, pages, chars = uploads.ingest("up-owner", self._file(name), "xoxb-test")
                self.assertTrue(doc_id.startswith("upload-"))
                self.assertEqual((pages, chars), (1, len(body)))
                self.assertIn(body, docs.fetch_doc("up-owner", "t", doc_id=doc_id))
                self.assertIn(doc_id, docs.list_docs("up-owner", "t"))
                self.assertNotIn(doc_id, docs.list_docs("up-other", "t"))
                with self.assertRaises(ValueError):
                    docs.fetch_doc("up-other", "t", doc_id=doc_id)

    def test_invalid_utf8_is_replaced_not_fatal(self):
        self.data = b"ok \xff\xfe end"
        doc_id, _, _ = uploads.ingest("up-u8", self._file("x.txt"), "tok")
        self.assertIn("ok �� end", docs.read_page(doc_id, 1, "up-u8"))

    @unittest.skipUnless(HAS_PYPDF, "pypdf not installed")
    def test_pdf_text_extracted(self):
        self.data = tiny_pdf("Launch is on Tuesday")
        doc_id, pages, _ = uploads.ingest("up-pdf", self._file("Plan.PDF"), "tok")
        self.assertEqual((doc_id, pages), ("upload-Plan", 1))
        self.assertIn("Launch is on Tuesday", docs.read_page(doc_id, 1, "up-pdf"))

    @unittest.skipUnless(HAS_PYPDF, "pypdf not installed")
    def test_broken_pdf_is_a_clear_error(self):
        self.data = b"%PDF-1.4 garbage"
        with self.assertRaises(uploads.UploadError):
            uploads.ingest("up-pdf", self._file("bad.pdf"), "tok")

    def test_unsupported_type_refused_before_download(self):
        self.data = b"MZ"
        for name in ("tool.exe", "noext", "pic.png"):
            with self.subTest(name), self.assertRaisesRegex(uploads.UploadError, r"\.txt"):
                uploads.ingest("up-x", self._file(name), "tok")
        self.assertEqual(self.fetched, [])

    def test_empty_file_refused(self):
        self.data = b""
        with self.assertRaisesRegex(uploads.UploadError, "empty"):
            uploads.ingest("up-x", self._file("empty.txt", size=0), "tok")
        self.data = b"  \n\n "
        with self.assertRaisesRegex(uploads.UploadError, "no text"):
            uploads.ingest("up-x", self._file("blank.txt"), "tok")

    def test_max_file_bytes_refuses_large_files(self):
        config.MAX_FILE_BYTES = 10
        self.data = b"x" * 11
        with self.assertRaisesRegex(uploads.UploadError, "10-byte limit"):
            uploads.ingest("up-x", self._file("big.txt"), "tok")
        self.assertEqual(self.fetched, [])  # declared size checked before download
        self.data = b"small"
        uploads.ingest("up-x", self._file("small.txt"), "tok")
        self.assertEqual(self.fetched[-1][2], 10)  # cap also enforced on the read

    def test_traversal_filename_stays_in_user_dir(self):
        self.data = b"payload"
        for name in ("../../etc/passwd.txt", "..\\..\\evil.md", "/abs/x.txt", "a/../../b.csv"):
            with self.subTest(name):
                doc_id, _, _ = uploads.ingest("up-tr", self._file(name), "tok")
                self.assertRegex(doc_id, r"^upload-[A-Za-z0-9_-]+$")
        files = [p.relative_to(self.dir) for p in Path(self.dir).rglob("*") if p.is_file()]
        self.assertTrue(files)
        self.assertTrue(all(p.parts[0] == "up-tr" and len(p.parts) == 2 for p in files), files)

    def test_reupload_replaces_doc_and_drops_stale_notes(self):
        self.data = b"old"
        doc_id, _, _ = uploads.ingest("up-re", self._file("r.txt"), "tok")
        c = db.conn()
        with c:
            c.execute(
                "INSERT INTO doc_notes (user_id, thread_id, doc_id, page, kind, content)"
                " VALUES (?, 't', ?, 1, 'summary', 'old notes')",
                ("up-re", doc_id),
            )
        self.data = b"new"
        uploads.ingest("up-re", self._file("r.txt"), "tok")
        self.assertEqual(docs.read_page(doc_id, 1, "up-re"), "new")
        self.assertEqual(doc_notes.noted_pages("up-re", "t", doc_id), set())

    def test_reupload_drops_stale_notes_in_every_thread(self):
        # AC-B2-3 edge: notes are per thread, but a re-upload must clear all threads' notes.
        self.data = b"old"
        doc_id, _, _ = uploads.ingest("up-re2", self._file("r.txt"), "tok")
        c = db.conn()
        with c:
            c.executemany(
                "INSERT INTO doc_notes (user_id, thread_id, doc_id, page, kind, content)"
                " VALUES ('up-re2', ?, ?, 1, 'key_fact', 'old fact')",
                [("a", doc_id), ("b", doc_id), (None, doc_id)],
            )
        self.data = b"new"
        uploads.ingest("up-re2", self._file("r.txt"), "tok")
        for t in ("a", "b"):
            self.assertEqual(doc_notes.noted_pages("up-re2", t, doc_id), set())
            self.assertNotIn("old fact", doc_notes.search_doc_notes("up-re2", t, "old fact"))
        n = c.execute("SELECT COUNT(*) FROM doc_notes WHERE user_id = 'up-re2'").fetchone()[0]
        self.assertEqual(n, 0)  # legacy NULL-thread rows go too

    def test_uploads_listed_next_to_seeded_docs_and_in_note_search(self):
        (Path(self.dir) / "up-sd").mkdir()
        (Path(self.dir) / "up-sd" / "upload-Q3-plan.md").write_text("upload")
        self.assertIn("Q3-plan", docs.available_docs("up-sd"))
        self.assertIn("upload-Q3-plan", docs.available_docs("up-sd"))
        self.assertIn("upload-Q3-plan", doc_notes.search_doc_notes("up-sd", "t", "zzz"))
        self.assertEqual(docs.page_count("upload-Q3-plan", "up-sd"), 1)


class DownloadTest(unittest.TestCase):
    def test_non_slack_urls_refused_before_any_request(self):
        for url in (
            "http://files.slack.com/x",
            "https://evilslack.com/x",
            "https://files.slack.com.evil.com/x",
            "https://files.slack.com@evil.com/x",
            "file:///etc/passwd",
            "",
        ):
            with self.subTest(url), self.assertRaisesRegex(uploads.UploadError, "not a Slack"):
                uploads.download(url, "xoxb-secret")
        self.assertTrue(uploads.is_slack_url("https://files.slack.com/files-pri/a/b"))

    def test_redirect_off_slack_refused(self):
        h = uploads._SlackOnlyRedirects()
        req = urllib.request.Request(URL, headers={"Authorization": "Bearer xoxb-secret"})
        with self.assertRaises(urllib.error.HTTPError):
            h.redirect_request(req, None, 302, "Found", {}, "https://evil.example/steal")
        ok = h.redirect_request(req, None, 302, "Found", {}, "https://files.slack.com/y")
        self.assertEqual(ok.full_url, "https://files.slack.com/y")


if __name__ == "__main__":
    unittest.main()
