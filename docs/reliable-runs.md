# Reliable runs in one backend

The revised scope keeps the existing backend, asynchronous agent execution,
and WebSocket progress delivery. It does not add a worker service or a job queue.

Accepted runs belong to a user and a chat. Closing a connection must only stop
progress delivery. Reopening the chat restores saved output and follows the
existing run. That chat stays locked to new submissions until its run finishes.
Stop and approval controls remain available, and other chats remain usable.

Backend shutdown interrupts unfinished execution. Execution checkpoints and
safe restart continuation are deferred. Database access continues to use a
verified user token that lasts for the entire bounded run.

Implementation proceeds through request identity, chat restoration, credential
refresh before acceptance, shared capacity limits, and interruption verification.
