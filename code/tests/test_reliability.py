"""Phase 5A reliability: atomic output writes (A1), the evidence-integrity gate shared by the CLI and
the evaluator (A2), and scratch-path containment for experimental modes (A4).

Fixtures are synthetic (example.test addresses, invented resource ids); nothing here is derived
from a sample or evaluation case.
"""
import importlib.util
import json
import os
import shutil
import struct
import sys
import tempfile
import unittest
import zlib
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "evaluation"))
REPO = os.path.dirname(os.path.dirname(HERE))

from ghostledger import pipeline, safeio  # noqa: E402
from ghostledger.evidence import NO_TEXT, EvidenceIndex, png_defect  # noqa: E402
from ghostledger.integrity import FATAL, IntegrityError, check, report  # noqa: E402
from ghostledger.io import load_dataset  # noqa: E402
from ghostledger.pipeline import CaseResult, serialize_output, write_output  # noqa: E402
from test_synthetic import REQ_COLS, request, user, write_csv  # noqa: E402

_spec = importlib.util.spec_from_file_location("ghostledger_cli", os.path.join(os.path.dirname(HERE), "main.py"))
cli = importlib.util.module_from_spec(_spec)  # by path: another top-level "main" module may already be imported
_spec.loader.exec_module(cli)

PRIOR = "case_id,deletion_status,steps_json\ncase_1,COMPLETE,[]\n"


def png(w=1, h=1):
    def chunk(kind, body):
        return struct.pack(">I", len(body)) + kind + body + struct.pack(">I", zlib.crc32(kind + body) & 0xffffffff)
    raw = b"".join(b"\x00" + b"\x00" * w for _ in range(h))
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 0, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b""))


def results():
    st = {"step_no": 1, "phase": "VERIFICATION", "system_id": "account_service", "resource_id": "account_90001",
          "operation": "VERIFY_RECORD_ABSENT", "state": "ABSENT", "evidence_id": "evidence_x"}
    return [CaseResult("case_2", "COMPLETE", [st]), CaseResult("case_10", "NOT_APPLICABLE", []),
            CaseResult("case_1", "INCOMPLETE", [])]


class AtomicOutputWrite(unittest.TestCase):
    """A1: the destination holds either the complete previous file or the complete new one."""

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.out = os.path.join(self.dir, "output.csv")
        with open(self.out, "w", encoding="utf-8", newline="") as f:
            f.write(PRIOR)

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def prior_kept(self):
        with open(self.out, encoding="utf-8", newline="") as f:
            self.assertEqual(f.read(), PRIOR)
        self.assertEqual(os.listdir(self.dir), ["output.csv"], "temporary file left behind")

    def test_successful_write_replaces_and_sorts(self):
        write_output(results(), self.out)
        with open(self.out, encoding="utf-8", newline="") as f:
            text = f.read()
        self.assertEqual([l.split(",")[0] for l in text.splitlines()], ["case_id", "case_1", "case_2", "case_10"])
        self.assertEqual(text, serialize_output(results()))
        self.assertEqual(os.listdir(self.dir), ["output.csv"])

    def test_interrupted_serialization_writes_nothing(self):
        real, n = pipeline.to_json, [0]

        def flaky(steps):
            n[0] += 1
            if n[0] == 2:
                raise OSError("injected: serialization failed on the second row")
            return real(steps)
        with mock.patch.object(pipeline, "to_json", flaky), self.assertRaises(OSError):
            write_output(results(), self.out)
        self.prior_kept()

    def test_disk_full_during_write(self):
        real = os.fdopen

        class Half:
            def __init__(self, f):
                self.f = f

            def __enter__(self):
                return self

            def __exit__(self, *a):
                self.f.close()

            def write(self, t):
                self.f.write(t[: len(t) // 2])
                raise OSError(28, "injected: No space left on device")
        with mock.patch.object(safeio.os, "fdopen", lambda *a, **k: Half(real(*a, **k))), self.assertRaises(OSError):
            write_output(results(), self.out)
        self.prior_kept()

    def test_fsync_and_replace_failures(self):
        for name in ("fsync", "replace"):
            with self.subTest(name), mock.patch.object(safeio.os, name, side_effect=OSError(5, "injected")), \
                    self.assertRaises(OSError):
                write_output(results(), self.out)
            self.prior_kept()

    def test_interrupt_cleans_up(self):
        with mock.patch.object(safeio.os, "replace", side_effect=KeyboardInterrupt), self.assertRaises(KeyboardInterrupt):
            write_output(results(), self.out)
        self.prior_kept()

    def test_new_destination_not_created_on_failure(self):
        os.remove(self.out)
        with mock.patch.object(safeio.os, "replace", side_effect=OSError(5, "injected")), self.assertRaises(OSError):
            write_output(results(), self.out)
        self.assertEqual(os.listdir(self.dir), [])

    @unittest.skipUnless(os.name == "nt", "Windows sharing semantics")
    def test_windows_destination_held_open(self):
        # a reader without delete sharing blocks os.replace on Windows: documented, not hidden
        with open(self.out, "rb"), self.assertRaises(PermissionError):
            write_output(results(), self.out)
        self.prior_kept()

    def test_contract_violations_refused(self):
        bad = [[CaseResult("case_1", "COMPLETE", []), CaseResult("case_1", "COMPLETE", [])],
               [CaseResult("case_1", "UNRESOLVED", [])],
               [CaseResult("case_1", "NOT_APPLICABLE", results()[0].steps)],
               [CaseResult("case_1", "COMPLETE", [dict(results()[0].steps[0], step_no=2)])]]
        for rs in bad:
            with self.subTest(rs[0].status), self.assertRaises(ValueError):
                write_output(rs, self.out)
            self.prior_kept()


def mini_dataset(tmp, files):
    """files: {file_id: (relative path, bytes, captured_at)} plus a one-request dataset around them."""
    shutil.copy(os.path.join(REPO, "dataset", "deletion_rules.csv"), os.path.join(tmp, "deletion_rules.csv"))
    write_csv(os.path.join(tmp, "user_identities.csv"), ["user_id", "name", "email", "phone", "email_hash", "updated_at"],
              [user("user_90001", "mira.koval@example.test", "+1 555 0100 222")])
    write_csv(os.path.join(tmp, "user_audit_log.csv"),
              ["audit_id", "user_id", "event_time", "field_name", "old_value", "new_value"], [])
    write_csv(os.path.join(tmp, "system_activity.csv"),
              ["activity_id", "event_time", "event_type", "source_system", "target_system", "resource_id",
               "reported_result", "details"], [])
    req = [request("case_1", "mira.koval@example.test", "2026-09-01T00:00:00Z")]
    for name in ("deletion_requests.csv", "sample_deletion_requests.csv"):
        write_csv(os.path.join(tmp, name), REQ_COLS, req)
    with open(os.path.join(tmp, "sample_output.csv"), "w", encoding="utf-8", newline="") as f:
        f.write("case_id,deletion_status,steps_json\ncase_1,COMPLETE,[]\n")
    manifest = []
    for fid, (rel, data, cap) in files.items():
        if data is not None:
            os.makedirs(os.path.dirname(os.path.join(tmp, rel)), exist_ok=True)
            with open(os.path.join(tmp, rel), "wb") as f:
                f.write(data)
        manifest.append({"file_id": fid, "file_path": rel, "origin": "synthetic_fixture", "captured_at": cap})
    write_csv(os.path.join(tmp, "evidence_files.csv"), ["file_id", "file_path", "origin", "captured_at"], manifest)
    return tmp


def records(recs, cap="2026-08-10T00:00:00Z"):
    return json.dumps({"collection": "customer_profiles", "captured_at": cap, "records": recs}).encode()


ACCOUNT = {"resource_id": "account_90001", "email": "mira.koval@example.test", "account_status": "ACTIVE"}
CAP = "2026-08-10T00:00:00Z"


def blank_pdf():
    import io
    import pypdf
    w, buf = pypdf.PdfWriter(), io.BytesIO()
    w.add_blank_page(612, 792)
    w.write(buf)
    return buf.getvalue()


class IntegrityGate(unittest.TestCase):
    """A2: every manifest file gets one integrity class; required artifacts that are missing,
    malformed or unparsed stop the run instead of becoming an empty evidence set."""

    FILES = {
        "ev_ok": ("evidence/records/ok.json", records([ACCOUNT]), CAP),
        "ev_late": ("evidence/records/late.json", records([dict(ACCOUNT, recorded_at="2026-08-12T00:00:00Z")]), CAP),
        "ev_png": ("evidence/images/screen.png", png(3, 2), CAP),
        "ev_txt_empty": ("evidence/records/notes.txt", b"handover notes: nothing listed\n", CAP),
        "ev_json_bad": ("evidence/records/bad.json", records([ACCOUNT])[:-5] + b"{oops", CAP),
        "ev_json_layout": ("evidence/records/layout.json", b'{"items": []}', CAP),
        "ev_log_enc": ("evidence/logs/app.log", b"2026-08-01T00:00:00Z resource_id=account_90001 \xff\xfe\n", CAP),
        "ev_sql_bad": ("evidence/databases/db.sql", b"CREATE TABLE customer_profiles(resource_id TEXT);\nINSERT INTO nowhere VALUES (;\n", CAP),
        "ev_png_bad": ("evidence/images/broken.png", png(3, 2)[:30], CAP),
        "ev_missing": ("evidence/records/gone.json", None, CAP),
        "ev_partial": ("evidence/records/partial.json", json.dumps({"snapshots": [
            {"collection": "customer_profiles", "captured_at": CAP, "records": [dict(ACCOUNT, resource_id="account_90002")]},
            {"captured_at": CAP, "records": []}]}).encode(), CAP),
        "ev_pdf_blank": ("evidence/documents/blank.pdf", None, CAP),
    }
    EXPECTED = {"ev_ok": "PARSED", "ev_late": "NEEDS_REVIEW", "ev_png": "OPTIONAL_UNSUPPORTED",
                "ev_txt_empty": "EMPTY_EXTRACTION", "ev_json_bad": "MALFORMED", "ev_json_layout": "MALFORMED",
                "ev_log_enc": "MALFORMED", "ev_sql_bad": "MALFORMED", "ev_png_bad": "MALFORMED",
                "ev_missing": "MISSING", "ev_partial": "FAILED_PARSE", "ev_pdf_blank": "EMPTY_EXTRACTION"}

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp()
        files = dict(cls.FILES, ev_pdf_blank=(cls.FILES["ev_pdf_blank"][0], blank_pdf(), CAP))
        mini_dataset(cls.tmp, files)
        cls.index = EvidenceIndex(load_dataset(cls.tmp, "deletion_requests.csv")).build()

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def test_each_file_classified(self):
        self.assertEqual({f: c for f, (c, _) in self.index.file_class.items()}, self.EXPECTED)
        self.assertEqual(self.index.file_class["ev_pdf_blank"][1], NO_TEXT)

    def test_fatal_set(self):
        r = report(self.index)
        self.assertEqual(set(r["fatal"]), {f for f, c in self.EXPECTED.items() if c in FATAL} | {"ev_pdf_blank"})
        self.assertEqual(set(r["review"]), {"ev_late"})
        self.assertEqual(set(r["empty"]), {"ev_txt_empty"})
        with self.assertRaises(IntegrityError) as e:
            check(self.index)
        self.assertIn("ev_json_bad MALFORMED", str(e.exception))

    def test_failed_file_contributes_nothing(self):
        # ev_partial added account_90002 from its first snapshot before failing on the second
        self.assertNotIn("account_90002", self.index.by_resource)
        self.assertFalse(any(o.file_id in ("ev_partial", "ev_json_bad") for o in self.index.observations))
        self.assertFalse(any(s.file_id == "ev_partial" for s in self.index.scopes))

    def test_late_record_keeps_both_times(self):
        o = next(o for o in self.index.observations if o.file_id == "ev_late")
        self.assertEqual((o.file_captured_at, o.observed_at), (CAP, "2026-08-12T00:00:00Z"))

    def test_png_structure(self):
        d = os.path.join(self.tmp, "evidence", "images")
        self.assertIsNone(png_defect(os.path.join(d, "screen.png")))
        self.assertIn("png defect", png_defect(os.path.join(d, "broken.png")))

    def test_shipped_dataset_passes_gate(self):
        idx = EvidenceIndex(load_dataset(os.path.join(REPO, "dataset"), "sample_deletion_requests.csv")).build()
        r = check(idx)
        self.assertEqual(r["fatal"], {})
        pngs = [f for f, m in idx.ds.manifest.items() if m["file_path"].endswith(".png")]
        self.assertTrue(pngs)
        self.assertTrue(all(idx.file_class[f][0] == "OPTIONAL_UNSUPPORTED" for f in pngs))


class GateEntryPoints(unittest.TestCase):
    """A2: both the CLI and the evaluator refuse a degraded index and leave earlier results untouched."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        os.makedirs(os.path.join(self.tmp, "ds"))
        self.ds = mini_dataset(os.path.join(self.tmp, "ds"), {
            "ev_ok": ("evidence/records/ok.json", records([ACCOUNT]), CAP),
            "ev_bad": ("evidence/records/bad.json", b"{oops", CAP)})

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_cli_refuses_and_keeps_prior_output(self):
        out = os.path.join(self.tmp, "output.csv")
        with open(out, "w", encoding="utf-8", newline="") as f:
            f.write(PRIOR)
        with self.assertRaises(SystemExit) as e:
            cli.main(["--dataset", self.ds, "--out", out])
        self.assertIn("ev_bad MALFORMED", str(e.exception.code))
        with open(out, encoding="utf-8", newline="") as f:
            self.assertEqual(f.read(), PRIOR)

    def test_evaluator_refuses_and_keeps_prior_report(self):
        import evaluate
        evd = os.path.join(self.tmp, "ev")
        os.makedirs(evd)
        with open(os.path.join(evd, "sample_eval.json"), "w") as f:
            f.write('{"prior": true}')
        with self.assertRaises(SystemExit) as e:
            evaluate.main(["--dataset", self.ds, "--out-dir", evd])
        self.assertIn("ev_bad MALFORMED", str(e.exception.code))
        with open(os.path.join(evd, "sample_eval.json")) as f:
            self.assertEqual(f.read(), '{"prior": true}')


class ScratchContainment(unittest.TestCase):
    """A4: experimental modes may write only inside <repo>/.scratch, by resolved-path containment."""

    def test_paths(self):
        s = cli.SCRATCH
        inside = [os.path.join(s, "phase5", "x.csv"), os.path.join(s, "phase4", "f", "out.csv"),
                  os.path.join(s, "a", "..", "b.csv")]
        outside = [os.path.join(cli.REPO, "output.csv"), s + "_x" + os.sep + "out.csv",
                   os.path.join(cli.REPO, ".scratchy", "o.csv"), os.path.join(s, "..", "output.csv"),
                   os.path.join(s, "phase5", "..", "..", "code", "o.csv"), os.path.join(tempfile.gettempdir(), "o.csv")]
        if os.name == "nt":
            inside.append(s.upper().replace(os.sep, "/") + "/phase5/x.csv")
            drive = "Z:" if not s.upper().startswith("Z:") else "Y:"
            outside.append(drive + "\\.scratch\\o.csv")
        for p in inside:
            self.assertTrue(cli.inside(p, s), p)
        for p in outside:
            self.assertFalse(cli.inside(p, s), p)

    def test_strict_mode_refused_outside_scratch(self):
        with self.assertRaises(SystemExit) as e:
            cli.main(["--temporal", "strict", "--out", cli.SCRATCH + "_sibling" + os.sep + "o.csv"])
        self.assertIn("experimental", str(e.exception.code))


if __name__ == "__main__":
    unittest.main()
