"""Evidence-integrity gate shared by the CLI (code/main.py) and the evaluator.

EvidenceIndex.build() classifies every manifest file as one of:

  PARSED                parsed; contributed observations or enumerated scopes
  OPTIONAL_UNSUPPORTED  structurally valid, but its modality has no backend in this run (PNG without OCR)
  EMPTY_EXTRACTION      read without error but yielded no resource sightings or scopes
  NEEDS_REVIEW          parsed, but a record time is later than the file's manifest capture time
  FAILED_PARSE          the parser could not run on it (no parser, dependency import failed, ...)
  MALFORMED             the content itself is broken (bad JSON/SQL/encoding/PDF stream/PNG structure)
  MISSING               the manifest names a file that does not exist

Fatal classes stop a run before anything is written, so a broken required artifact never turns
into an empty evidence set and from there into confident COMPLETE answers. EMPTY_EXTRACTION is
fatal only when a document produced no text at all (a broken or image-only PDF); readable content
that simply names no resource is reported and allowed. NEEDS_REVIEW is reported, not fatal: the
time conflict is in the participant data itself and every observation keeps both timestamps.
"""
from collections import Counter

from .evidence import NO_TEXT

CLASSES = ("PARSED", "OPTIONAL_UNSUPPORTED", "EMPTY_EXTRACTION", "NEEDS_REVIEW", "FAILED_PARSE", "MALFORMED", "MISSING")
FATAL = {"FAILED_PARSE", "MALFORMED", "MISSING"}


class IntegrityError(RuntimeError):
    def __init__(self, failures):
        self.failures = failures
        super().__init__("evidence integrity check failed, refusing to produce results: " +
                         "; ".join(f"{f} {c}: {d}" for f, (c, d) in sorted(failures.items())))


def fatal(cls, detail):
    return cls in FATAL or (cls == "EMPTY_EXTRACTION" and detail == NO_TEXT)


def report(index):
    """{'counts': {class: n}, 'fatal': {file_id: (class, detail)}, 'review': {...}, 'empty': {...}}."""
    fc = index.file_class
    missing = set(index.ds.manifest) - set(fc)
    out = {"counts": dict(Counter(c for c, _ in fc.values())),
           "fatal": {f: v for f, v in fc.items() if fatal(*v)},
           "review": {f: v for f, v in fc.items() if v[0] == "NEEDS_REVIEW"},
           "empty": {f: v for f, v in fc.items() if v[0] == "EMPTY_EXTRACTION" and not fatal(*v)},
           "import_diagnostics": index.import_diagnostics}
    for f in missing:  # a manifest entry the index never classified is itself a failure
        out["fatal"][f] = ("FAILED_PARSE", "not classified by EvidenceIndex.build()")
    return out


def check(index):
    """Raise IntegrityError if any required artifact is missing, malformed or unparsed."""
    r = report(index)
    if r["fatal"]:
        raise IntegrityError(r["fatal"])
    return r
