"""Async TCP client for the Sonance DSP binary protocol.

SCAFFOLD ONLY -- not implemented yet.
See ``docs/protocol.md`` for the verified protocol and the evidence behind it.

Design, driven by three measured device behaviours rather than by preference:

1. **Exactly one connection, held for the config entry's lifetime.**
   A second concurrent socket receives nothing *and* its replies are delivered
   to the first socket, corrupting that stream. This is a correctness
   requirement, not an optimisation.

2. **FIFO future correlation.** A single socket pipelines correctly -- N queries
   return N ordered replies. Push a Future per command onto an ``asyncio.Queue``;
   the reader task resolves the head Future per reply.

3. **Fixed-width reads, never readline().** Replies are exactly 50 bytes,
   NUL-padded, with no terminator::

       await asyncio.wait_for(reader.readexactly(REPLY_LENGTH), timeout)

   A timeout means "group does not exist" during enumeration, and "device fault"
   afterwards. The same read on a line-oriented reader hangs forever.

Also required:

* Exponential-backoff reconnect capped at 30 s, with a connection-state callback
  so entities can flip availability.
* Strip trailing NULs before parsing; match with the RE_* patterns in const.py,
  which tolerate both the query and command-echo whitespace forms.
* Refuse to emit any opcode in ``FORBIDDEN_OPCODES``. Assert it at the frame
  builder, not just by convention -- those opcodes echo success while silently
  failing, so a mistake here is invisible until someone reads the topology back.
"""

from __future__ import annotations
