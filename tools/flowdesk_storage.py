"""Offline maintenance for FlowDesk candle stores, without market credentials.

Examples:
    python -m tools.flowdesk_storage --database ~/.flowdesk/finam_candles_v1.sqlite3 --backup
    python -m tools.flowdesk_storage --database ~/.flowdesk/finam_candles_v1.sqlite3 --list
    python -m tools.flowdesk_storage --database ~/.flowdesk/finam_candles_v1.sqlite3 --verify
    python -m tools.flowdesk_storage --database PATH --restore SNAPSHOT --output NEWFILE

Recovery ALWAYS creates a new file; never swap/delete a working database.
"""
import argparse
import json
from pathlib import Path
from flowdesk.storage_safety import (
    backup_if_due, create_backup, recover_to_new_file, snapshots, verify_database,
)


def main():
    parser=argparse.ArgumentParser(description="FlowDesk SQLite maintenance (read-only unless backing up)")
    parser.add_argument("--database",required=True,type=Path)
    op=parser.add_mutually_exclusive_group(required=True)
    op.add_argument("--backup",action="store_true",help="verified WAL-consistent snapshot")
    op.add_argument("--list",action="store_true",help="list existing snapshots")
    op.add_argument("--verify",action="store_true",help="full SQLite integrity check")
    op.add_argument("--restore",type=Path,metavar="SNAPSHOT",help="restore to new file only")
    parser.add_argument("--output",type=Path,help="new destination; mandatory for --restore")
    args=parser.parse_args()
    if args.restore and args.output is None:
        parser.error("--restore requires --output NEWFILE")
    if args.output is not None and not args.restore:
        parser.error("--output is only used with --restore")
    if args.backup:
        result=create_backup(args.database)
    elif args.list:
        result={"snapshots":[str(p) for p in snapshots(args.database)]}
    elif args.verify:
        result={"candles":verify_database(args.database),"integrity":"ok"}
    else:
        # Never restore to the user's real market DB in-place.
        if args.output.resolve()==args.database.resolve():
            parser.error("Refusing to overwrite the original database")
        result=recover_to_new_file(args.restore,args.output)
    print(json.dumps(result,ensure_ascii=False,sort_keys=True))


if __name__=="__main__":
    main()
