"""Loading of the participant-facing CSV tables."""
import csv
import os
from dataclasses import dataclass, field


def read_csv(path):
    with open(path, encoding="utf-8", newline="") as f:
        return list(csv.DictReader(f))


@dataclass
class Dataset:
    root: str
    requests: list
    rules: list
    manifest: dict
    identities: list
    audit_log: list
    activity: list
    extra: dict = field(default_factory=dict)

    def path(self, rel):
        return os.path.join(self.root, rel)


def load_dataset(root, requests_file="deletion_requests.csv"):
    j = lambda name: os.path.join(root, name)
    return Dataset(
        root=root,
        requests=read_csv(j(requests_file)),
        rules=read_csv(j("deletion_rules.csv")),
        manifest={r["file_id"]: r for r in read_csv(j("evidence_files.csv"))},
        identities=read_csv(j("user_identities.csv")),
        audit_log=read_csv(j("user_audit_log.csv")),
        activity=read_csv(j("system_activity.csv")),
    )
