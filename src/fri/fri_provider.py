"""Which provider answers a model call, and the one handover to the other when the first can't answer at all."""

# The plan's tiers on Claude; the effort name carries over unchanged.
TIERS = {'gpt-6.2-sol': 'claude-opus-5-5', 'gpt-6.1-sol': 'claude-opus-5-5',
         'gpt-6.1-terra': 'claude-sonnet-5-5', 'gpt-6-luna': 'claude-haiku-5-5'}
# Dollars per million tokens: input, output, cache read, cache write (5 minutes). Web search is $10 per 1,000.
PRICES = {'claude-opus-5-5': (4.0, 20.0, 0.20, 5.0), 'claude-sonnet-5-5': (2.0, 10.0, 0.10, 2.5),
          'claude-haiku-5-5': (0.10, 0.50, 0.01, 0.125)}
WEB_SEARCH_MICROS = 10_000
CAPABILITY = 'model-provider'  # The controller says which provider is primary (provider on /chat/context).
# Every failover a receipt may name, as the controller takes them; 'refusal' is recorded, never failed over.
REASONS = ('rate_limited', 'overloaded', 'billing', 'auth', 'connection', 'usage_limit', 'refusal')
MAX_COST_MICROS = 1_000_000_000  # The most one receipt may carry.


class Failover(Exception):
    """Raised by a backend that produced no output: the other provider may be tried. `result` is the failed attempt to
    record, or None when nothing was sent (no key, the latch, no SDK)."""

    def __init__(self, reason, result=None):
        super().__init__(reason)
        self.reason, self.result = reason, result


def route(config, plan_model, effort):
    """[(provider, model, effort), ...]: the primary, then the fallback. `config` is what the controller returned
    (/chat/context): its `provider` and `claude_chat_model` ('' = the tier map); an older controller sends neither, so
    the plan stays primary. Plan model ids only: the controller never stores a Claude id in them."""
    claude = ('claude', config.get('claude_chat_model') or TIERS.get(plan_model, 'claude-opus-5-5'), effort)
    plan = ('chatgpt', plan_model, effort)
    return [claude, plan] if config.get('provider') == 'claude' else [plan, claude]


def cost_micros(model, fresh, output, read=0, written=0, searches=0):
    """Integer micro-dollars from a Claude response's usage: uncached input, output (thinking included), cache reads
    and cache writes, each at its own price, capped at what a receipt may carry. None for a model without a price."""
    if model not in PRICES:
        return None
    price_in, price_out, price_read, price_write = PRICES[model]
    return min(round(fresh*price_in + output*price_out + read*price_read + written*price_write)
               + searches*WEB_SEARCH_MICROS, MAX_COST_MICROS)


async def call(order, attempt, recheck=None, record=None):
    """attempt(provider, model, effort, gate) -> result. Tries order[0]; on Failover, tries order[1] once with
    gate=recheck, which that backend awaits after its own sign-in, right before its request: the non-waiting privacy
    checks, raising to stop with nothing sent. record(provider, model, effort, result, failover) is called for each
    attempt that sent something, as it ends, so a stop there still keeps the first one's usage.

    Returns (result, attempts). The result is the answering attempt's, or the last failed one's."""
    attempts, last = [], None
    for index, (provider, model, effort) in enumerate(order[:2]):
        try:
            result, failover = await attempt(provider, model, effort, recheck if index else None), None
        except Failover as exc:
            result, failover = exc.result, exc.reason
        if result is not None:
            attempts.append((provider, model, effort, result, failover))
            if record:
                record(provider, model, effort, result, failover)
            last = result
        if failover is None:
            return result, attempts
    return last, attempts
