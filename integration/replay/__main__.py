"""Print replay reports. Scripted mode is offline; --live-model calls the real provider.

    python -m replay [--scenario NAME ...] [--report FILE]
    python -m replay --live-model --auth SESSION.json --max-calls N [--model M --effort E]

Live mode uses a real subscription session, which may refresh and rewrite it. Use only a session
dedicated to evaluation, never the production session or a copy of its rotating refresh token.
Nothing is posted to Discord and no live controller or member history is touched.
"""

import argparse
import json
import sys

from .harness import failures, run
from .scenarios import SCENARIOS


def main(argv=None):
    parser = argparse.ArgumentParser(prog='python -m replay')
    parser.add_argument('--scenario', action='append', choices=[s.name for s in SCENARIOS])
    parser.add_argument('--report', help='write the JSON report here instead of stdout')
    parser.add_argument('--live-model', action='store_true', help='call the real provider (opt-in)')
    parser.add_argument('--auth', help='dedicated evaluation session file (live mode only)')
    parser.add_argument('--max-calls', type=int, help='hard provider-call budget across all scenarios')
    parser.add_argument('--model')
    parser.add_argument('--effort', default='high')
    args = parser.parse_args(argv)
    if args.max_calls is not None and args.max_calls < 1:
        parser.error('--max-calls must be positive')
    selected = [s for s in SCENARIOS if not args.scenario or s.name in args.scenario]
    options = {}
    if args.live_model:
        if not args.auth or not args.max_calls:
            parser.error('--live-model requires --auth and a positive --max-calls budget')
        import httpx

        from fri.subscription import Subscription
        # A factory: each scenario creates and closes its transport inside its own event loop.
        options.update(live=httpx.AsyncHTTPTransport, subscription=Subscription(args.auth))
    if args.model:
        options['chat_model'] = (args.model, args.effort)
    remaining, reports = args.max_calls, []
    for scenario in selected:
        if remaining is not None and remaining <= 0:
            reports.append({'scenario': scenario.name, 'skipped': 'model budget exhausted'})
            continue
        report = run(scenario, max_calls=remaining, **options)
        reports.append(report)
        if remaining is not None:
            remaining -= report['model_calls']
    output = json.dumps({'reports': reports, 'budget_remaining': remaining}, indent=2)
    if args.report:
        with open(args.report, 'w') as handle:
            handle.write(output+'\n')
    else:
        print(output)
    return 1 if failures(reports) else 0


if __name__ == '__main__':
    sys.exit(main())
