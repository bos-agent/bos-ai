# Protocol

`bos.protocol` is the cross-component message contract layer.

It currently owns:

- `Envelope`
- message type definitions
- command-type definitions related to message traffic

This layer is intentionally separate from `bos.core`.

## Why `protocol` Is Top-Level

`bos.protocol` is used by:

- actors
- channels
- mailbox implementations
- clients
- future inter-process transports

That makes it broader than the runtime core. It is the shared wire-level contract, even when everything is currently running in one process.

## Scope

`bos.protocol` should contain:

- data contracts passed between components
- message-kind enums or constants
- command/message classification relevant to transport or dispatch

`bos.protocol` should not contain:

- runtime service protocols such as `Mailbox` or `Agent`
- extension registries
- concrete default implementations
- harness or runner logic

Those belong in `bos.core`.
