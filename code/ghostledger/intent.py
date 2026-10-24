"""Request-intent classifier: does request_text authorize deletion?

Deterministic and multilingual. A request is a deletion request when it contains a deletion cue
and no explicit cue that the person wants the account/data kept or is asking for something else
(access, correction, export, marketing opt-out). Cues are general phrasings, not sample texts.
"""
import re

DELETION_CUES = [
    r"\bdelet(e|ed|ing|ion)\b", r"\beras(e|ed|ure)\b", r"\bremov(e|ed|al)\b", r"\b(profile|account) (is|was) gone\b",
    r"\bclose (my|this|the) account\b", r"\bclosed my account\b", r"\b(would like|want) it (closed|deleted)\b",
    r"\bforget me\b", r"\bwipe\b", r"\baccount[- ]deletion\b", r"\bno longer (need|use|want) (this|my) account\b",
    r"\bclean my personal details\b",
    # pt / es / fr / de / it
    r"exclus[ãa]o", r"\bexclu(ir|a)\b", r"\bapagar\b", r"\beliminar\b", r"\beliminaci[óo]n\b", r"\bborrar\b",
    r"\bsupresi[óo]n\b", r"\bsupprim", r"\bsuppression\b", r"\beffacer\b", r"\bl[öo]sch", r"\bcancellare\b",
    # ko / ja / zh
    r"삭제", r"탈퇴", r"削除", r"退会", r"删除", r"注销",
]
KEEP_CUES = [
    r"\bnot (asking|requesting) (for )?(a )?(deletion|erasure|to (delete|erase))\b",
    r"\bnot a request to (erase|delete)\b", r"\bdo(n't| not) (delete|close|remove|erase)\b",
    r"\bleave (the|my) (account|profile)\b", r"\bkeep (the|my) account\b", r"\bwithout removing\b",
    r"\b(marketing|mailing) lists? only\b", r"\bcontinue using\b", r"\bstill (need|want|use) (the|my|it)\b",
    r"\bleave .* in place\b", r"\bkeep .* as it is\b",
    r"no (quiero|deseo) (eliminar|borrar)", r"n[ãa]o (quero|desejo) (excluir|apagar)",
    r"ne (veux|souhaite) pas (supprimer|effacer)",
]
_del = [re.compile(p, re.I) for p in DELETION_CUES]
_keep = [re.compile(p, re.I) for p in KEEP_CUES]


def classify(text):
    """Return (authorizes_deletion: bool, reason: str)."""
    t = (text or "").replace("’", "'")
    keep = [p.pattern for p in _keep if p.search(t)]
    if keep:
        return False, "keep/non-deletion cue: " + keep[0]
    dele = [p.pattern for p in _del if p.search(t)]
    if dele:
        return True, "deletion cue: " + dele[0]
    return False, "no deletion cue"
