"""`shared/` を各エージェントのソースディレクトリへ複製する。

Hosted Agent のデプロイ (`azd deploy` / `azd ai agent init` の ZIP 化) は
**エージェントのソースディレクトリ配下のみ**をパッケージする。
そのため親ディレクトリにある `shared/` はそのままでは同梱されない。

このスクリプトを deploy 前に実行し、`agents/<impl>/shared/` を最新化する。
生成物は `.gitignore` 対象 (正本は常にリポジトリ直下の `shared/`)。

使い方::

    python tools/sync_shared.py
    python tools/sync_shared.py --check    # 差分があれば exit code 1
"""

from __future__ import annotations

import argparse
import filecmp
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "shared"
TARGETS = [
    ROOT / "agents" / "maf" / "shared",
    ROOT / "agents" / "copilot" / "shared",
]
FILES = ["__init__.py", "prompts.py", "tracing.py", "inputs.json"]


def sync(check_only: bool) -> int:
    missing = [name for name in FILES if not (SOURCE / name).is_file()]
    if missing:
        print(f"ERROR: source files missing in {SOURCE}: {', '.join(missing)}")
        return 2

    stale: list[str] = []
    for target in TARGETS:
        target.mkdir(parents=True, exist_ok=True)
        for name in FILES:
            src = SOURCE / name
            dst = target / name
            if dst.is_file() and filecmp.cmp(src, dst, shallow=False):
                continue
            stale.append(str(dst.relative_to(ROOT)))
            if not check_only:
                shutil.copy2(src, dst)

    if check_only:
        if stale:
            print("Out of date (run `python tools/sync_shared.py`):")
            for item in stale:
                print(f"  - {item}")
            return 1
        print("shared/ copies are up to date.")
        return 0

    if stale:
        print(f"Synced {len(stale)} file(s):")
        for item in stale:
            print(f"  - {item}")
    else:
        print("Nothing to sync.")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Sync shared/ into agent source dirs")
    parser.add_argument("--check", action="store_true", help="差分検出のみ (書き込まない)")
    args = parser.parse_args()
    return sync(check_only=args.check)


if __name__ == "__main__":
    sys.exit(main())
