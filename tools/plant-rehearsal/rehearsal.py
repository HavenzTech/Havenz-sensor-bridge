#!/usr/bin/env python3
"""
The plant rehearsal: the whole AHI deployment, simulated on one machine.

    python rehearsal.py up                 bring the plant up (own database, backend, readers, agent, apps)
    python rehearsal.py seed               register the plant through the real API; write the manifest
    python rehearsal.py run                run every scenario; write timeline, results, findings, report
    python rehearsal.py run leak emergency run only the named scenarios
    python rehearsal.py pairing-codes      fresh pairing codes for screens that are not paired
    python rehearsal.py report             rebuild REPORT.md / findings.md from a run's results
    python rehearsal.py reset              empty the plant again without rebuilding (then `seed`)
    python rehearsal.py status             what is running
    python rehearsal.py down               remove all of it (run folders are kept)

See README.md beside this file.
"""

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from plant import config, stack  # noqa: E402


def main():
    ap = argparse.ArgumentParser(prog="rehearsal.py", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="command", required=True)

    paths = config.default_paths()
    up = sub.add_parser("up", help="bring the plant up")
    up.add_argument("--bms", default=None, help=f"HavenzBMS checkout to build (default {paths['bms']})")
    up.add_argument("--dashboards", default=None, help=f"wall app checkout (default {paths['dashboards']})")
    up.add_argument("--door", default=None, help=f"door app checkout (default {paths['door']})")
    up.add_argument("--no-apps", action="store_true", help="do not start the wall and door apps")
    up.add_argument("--rebuild", action="store_true", help="rebuild images without the cache")

    seed = sub.add_parser("seed", help="register the plant through the real API and write the manifest")
    seed.add_argument("--people", type=int, default=60)
    seed.add_argument("--paced", action="store_true",
                      help="skip the bulk grant and enrol people two at a time (the bulk result is a known finding)")
    seed.add_argument("--resume", action="store_true", help="continue a seed that stopped part-way")
    seed.add_argument("--bulk-patience", type=int, default=600, metavar="SECONDS",
                      help="how long the bulk grant is given before its result is recorded (default 600)")

    run = sub.add_parser("run", help="run scenarios")
    run.add_argument("scenarios", nargs="*", help="names (default: all, in order)")
    run.add_argument("--run", dest="run_dir", default=None, help="run folder (default: the current one)")
    run.add_argument("--soak", default=None, metavar="30m", help="also run the soak for this long")
    run.add_argument("--list", action="store_true", help="list the scenarios and exit")
    run.add_argument("--wait", action="store_true", help="if another run is in progress, wait for it instead of refusing")

    codes = sub.add_parser("pairing-codes", help="mint fresh pairing codes for unpaired screens")
    codes.add_argument("--run", dest="run_dir", default=None)
    codes.add_argument("--all", action="store_true", help="also re-mint for screens that are paired")
    codes.add_argument("--screen", action="append", default=[], help="only this screen name (repeatable)")

    rep = sub.add_parser("report", help="rebuild the report from a run's results")
    rep.add_argument("--run", dest="run_dir", default=None)

    sub.add_parser("reset", help="empty the plant (new database, blank readers, unpaired agent); keeps images and apps")
    sub.add_parser("status", help="what is running")
    sub.add_parser("down", help="remove the rehearsal stack")

    args = ap.parse_args()

    if args.command == "up":
        stack.up(args.bms, args.dashboards, args.door, apps=not args.no_apps, rebuild=args.rebuild)
    elif args.command == "down":
        stack.down()
    elif args.command == "status":
        stack.status()
    elif args.command == "reset":
        stack.reset_data()
    elif args.command == "seed":
        from plant import seed as seed_mod
        seed_mod.seed(people=args.people, paced=args.paced, resume=args.resume,
                      bulk_patience=args.bulk_patience)
    elif args.command == "run":
        from plant import runner
        if args.list:
            runner.list_scenarios()
            return
        sys.exit(runner.run(args.scenarios, run_dir=args.run_dir, soak=args.soak, wait=args.wait))
    elif args.command == "pairing-codes":
        from plant import seed as seed_mod
        seed_mod.pairing_codes(run_dir=args.run_dir, include_paired=args.all, only=args.screen)
    elif args.command == "report":
        from plant import report
        report.build(run_dir=args.run_dir)


if __name__ == "__main__":
    main()
