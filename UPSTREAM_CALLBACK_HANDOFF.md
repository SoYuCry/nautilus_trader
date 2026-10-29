# Synchronous OrderInitialized callback and Python strategy borrowing

This is a local reproduction and discussion handoff, not a report that the latest upstream revision
is affected. Nothing has been posted to the upstream repository.

## Suggested issue title

Reading strategy.cache from a synchronous OrderInitialized callback raises a borrow error

## Observed behavior

Our Python strategy calls `submit_order()` from a normal runtime callback. Before the method returns,
`on_order_event(OrderInitialized)` runs synchronously. Accessing the public `self.cache` property in
that callback raises:

```text
RuntimeError: Already mutably borrowed
```

The native callback wrapper logs the exception and the submission continues. In our live sample,
15 new orders subsequently received Submitted/Accepted events, but their initialization audit records
were missing. Later asynchronous callbacks were not observed to fail this way.

## Environment actually tested

- Linux, Python 3.14.6, Nautilus Python package reports `2.0.0rc6`.
- Native extension retained from local upstream-based build `2bccb0cb92`.
- Runtime Python application commit at observation: `d6e2f26194` (private research checkout).
- This runtime is not automatically rebuilt when application code is updated.
- No claim is made about the latest upstream `develop`; please test there and record its exact SHA.

## Minimal standalone reproduction

Run the adjacent [reproduce_callback_borrow.py](reproduce_callback_borrow.py) in a current Nautilus
environment. It imports only Nautilus and the Python standard library, so it can be copied outside
this repository:

```bash
python reproduce_callback_borrow.py
```

It creates a native SANDBOX node without any data or execution clients. A timer submits a limit
order for a dummy instrument, and the initialization callback only attempts to obtain `self.cache`.
No network, credentials, Redis, or venue orders are involved.

Local output:

```json
{
  "version": "2.0.0rc6",
  "initialized_seen": true,
  "cache_access_ok": false,
  "failure": "RuntimeError: Already mutably borrowed",
  "execution_clients": 0
}
```

`INSTRUMENT_NOT_FOUND` after the initialization callback is expected: the deliberately empty node
has no dummy instrument. It is not the borrow error. This keeps the reproducer free of adapters and
venue-specific setup; a follow-up regression should also exercise a valid instrument/account.

## Local source trace

- `crates/trading/src/python/strategy.rs::py_submit_order` takes `&mut self` and enters the native
  `Strategy::submit_order` path.
- Native submission publishes OrderInitialized before that Python method returns.
- `dispatch_on_order_event` calls Python synchronously.
- `py_cache` takes `&self`; PyO3 rejects access while the original mutable receiver borrow remains.

The native Cache borrow used to add the order has already been released before publication in the
inspected submission path. The traceback therefore should not be described simply as a Redis lock
or a multithreaded Cache race: the Python component receiver reentry is the relevant boundary.

## Questions for upstream

1. Should read-only strategy resources be accessible from every canonical order callback,
   including synchronous OrderInitialized delivery?
2. Is this already addressed by current callback ownership work, or intentionally unsupported?
3. If a change is needed, what is the smallest binding/dispatch fix that preserves initialization
   before submission, recipient ordering, and native/Python parity?

The upstream [callback dispatch contract](https://github.com/nautechsystems/nautilus_trader/blob/develop/docs/developer_guide/callback_dispatch.md)
distinguishes queued-delivery design requirements from existing synchronous behavior. Do not assume
that queued callback activation is complete, or change all delivery ordering to fix this one symptom.

Do not disable borrow checking, swallow the error in the framework, or retry while the enclosing
borrow is still held. Add a regression using the actual native submission/callback path, not a
direct call on a mock strategy.

## Local application workaround

Our strategy audit now uses only event fields and its existing Python tick-observation dictionary.
An absent observed tick is recorded as null, not guessed. The quote decision still records the exact
tick read from the native instrument before submission. The audit labels its tick source explicitly.
OrderInitialized is logged without triggering another quote evaluation; subsequent execution events
retain their existing handling. The existing native application-message publish API is reused.

This is a small audit fix, not a second order state machine and not a replacement for an upstream
discussion about callback resource access. It does not modify the separate resolution-watch timeout
or the Polymarket `market_resolved` to InstrumentClose conversion.
