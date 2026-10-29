# User-selected August 20 restart

Same fixed rules as average_down_method.md; reset cash to 10,000 USDT and start
flat on August 20, 2026 UTC. Use August 19 23:00–23:59 only for the first opening
baseline. No earlier position or P&L is carried over. End September 11 09:00 UTC.
The start date was selected after viewing prior results, so this is a conditional
subperiod analysis, not untouched out-of-sample validation.

Four closed baskets earned 102.77 USDT after costs. One remaining five-layer
basket has estimated net liquidation P&L of -71.13. Combined net +31.64 USDT,
or +0.316% of initial capital. Price P&L +96.90, funding -23.25, total paid and
estimated exit transaction costs -42.01. Maximum minute-sampled equity drawdown
267.85 USDT; worst open basket net P&L -257.14; longest basket held 336.4 hours
(14.02 days). All four closed baskets were profitable, but that excludes the
unresolved losing basket and is not an unconditional 100% strategy win rate.

Reproduce: `python scripts/hynix_average_down.py --start 2026-08-20 --suffix _aug20`.
