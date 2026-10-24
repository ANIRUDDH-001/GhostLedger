"""GhostLedger CLI: audit every deletion request and write output.csv at the repository root.

    python code/main.py                       # full evaluation set -> ./output.csv
    python code/main.py --requests sample_deletion_requests.csv --out .scratch/sample_pred.csv
"""
import argparse
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from ghostledger.io import load_dataset  # noqa: E402
from ghostledger.pipeline import Auditor, Config, write_output  # noqa: E402

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--dataset", default=os.path.join(REPO, "dataset"))
    p.add_argument("--requests", default="deletion_requests.csv")
    p.add_argument("--out", default=os.path.join(REPO, "output.csv"))
    p.add_argument("--identity-horizon", choices=["any", "audit"], default="any")
    p.add_argument("--all-accounts", action="store_true", help="keep every linked account, not only the latest")
    p.add_argument("--drop-stale-absent", action="store_true")
    p.add_argument("--use-candidates", action="store_true", help="diagnostic: allow workflow-correlation links")
    a = p.parse_args(argv)
    t0 = time.perf_counter()
    ds = load_dataset(a.dataset, a.requests)
    cfg = Config(a.identity_horizon, not a.all_accounts, a.drop_stale_absent, a.use_candidates)
    auditor = Auditor(ds, cfg)
    results = auditor.run()
    write_output(results, a.out)
    counts = {}
    for r in results:
        counts[r.status] = counts.get(r.status, 0) + 1
    print(f"wrote {len(results)} rows to {a.out} {counts} in {time.perf_counter() - t0:.1f}s; "
          f"observations={len(auditor.index.observations)} unparsed_files="
          f"{sum(1 for v in auditor.index.file_status.values() if v != 'parsed')}; model_calls=0")
    return results


if __name__ == "__main__":
    main()
