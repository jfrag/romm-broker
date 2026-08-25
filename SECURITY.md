# Security

This container streams a full Linux desktop. `emulator: "desktop"` starts
`selkies-desktop` the same way any other emulator session starts, so anyone
who can drive that session has a real terminal (foot, by default) and can run
anything the container's user can run. That is not a bug, it is what desktop
mode is for: configuring emulators through the GUI. It does mean the trust
boundary around this container is "whoever can reach a session has a shell,"
not "an app with an API surface." Treat it that way when you deploy it,
especially the moment it stops being LAN-only.

## What the broker already enforces

Read this before adding anything, it is already covering part of the problem:

- **The process refuses to start without `BROKER_SECRET`**, unless
  `BROKER_DEV_MODE=true` is set explicitly ([main.py](webstation_broker/main.py)).
  Every session-lifecycle endpoint (`activate`, `join`, save/load state,
  swap-disc, state-file, memory-card, exports, imports, status) requires the
  matching `X-Broker-Secret` header once it is set.
- **`BROKER_DEV_MODE=true` disables that check on purpose**, for local
  development with a source mount. The broker logs a warning every time it
  starts this way. Never set it on anything reachable outside your own
  workstation.
- **Spawned emulators, including the desktop session, get a stripped
  environment.** `BROKER_SECRET`, `SELKIES_MASTER_TOKEN`, `GITHUB_TOKEN`, and
  anything shaped like `*_SECRET` / `*_TOKEN` / `*_PASSWORD` / `*_KEY` are
  removed before launch ([base.py](webstation_broker/emulators/base.py)). A
  terminal opened inside a desktop session cannot read the broker's own
  secret back out of its environment. This exists because RetroArch loads
  third-party cores with no sandboxing; the desktop session gets the same
  protection as a side effect, not as its main purpose, so do not rely on it
  alone.

None of this makes the terminal safe to expose. It means the credential that
gates reaching it is not also sitting in reach once you are inside.

## The real boundary: `BROKER_SECRET`

`BROKER_SECRET` is the single thing standing between the public internet and
an interactive shell in this container, once `activate` is reachable at all.
Treat it like a root password, not an API key:

- Generate it randomly (`openssl rand -hex 32` or equivalent), not a phrase
  you can remember.
- Never commit it, log it, or put it in a Dockerfile `ENV` line that lands in
  an image layer. Pass it at runtime.
- Rotate it if it may have been exposed (shared in chat, pasted into a bug
  report, visible in a process listing on a shared host).
- One secret authorizes every session type this broker knows, `desktop`
  included. This repo has no separate "who may request desktop mode" concept.
  If you need that boundary, it has to come from whatever calls `activate`
  (RomM's own permission model), not from the broker.

## Network isolation

Assume any session, not just desktop, is one exploited emulator away from a
shell (RetroArch cores and every other emulator here are unsandboxed native
code parsing untrusted ROM files). Isolate the container as if that shell
already exists:

- Put it on its own network segment or VLAN with no route to anything it
  does not need: no NAS shares beyond the ROM library mount, no other
  containers, no management interfaces.
- Mount only `ROM_ROOT` and `/config`. Nothing broader. Mount `ROM_ROOT`
  read-only if the broker's write path (state files under the save subtrees)
  does not need the whole tree writable.
- Run rootless, drop capabilities you are not using, never `--privileged`,
  never mount the container runtime's own socket into it.
- Give it a resource ceiling (CPU, memory) so a session that goes wrong is
  a contained problem, not a host-wide one.

## Reverse proxy

[Reverse proxy](https://romm-streaming.github.io/romm-broker/docs/deployment/reverse-proxy)
covers the mount mechanics (prefix handling, websocket upgrades, per-proxy
recipes). Two more things that only matter once this is public:

**Terminate real TLS at the proxy, never at the container.** Port 3001's
certificate is self-signed; it exists for the hop between the proxy and the
container, not for a browser to trust directly. The proxy is the only thing
that should ever hold a certificate a browser is meant to validate.

**The proxy is the actual internet-facing surface, so the controls the
broker does not implement belong there.** The broker has no rate limiting
and no IP allowlisting by design (this repo's scope stops at the session
API); a public deployment needs both in front of it, particularly on
`activate` and `join`. A secret is not a rate limit: a leaked or brute-forced
`BROKER_SECRET` should not also get unlimited attempts.

**Session and controller tokens ride in the URL** (`?token=...`), by design,
for the room links this broker hands out. That is fine on a LAN. On the
public internet it means the token sits in browser history, `Referer`
headers to any resource the room page loads cross-origin, and your proxy's
access logs. A leaked controller token during an active desktop session is
a shell handoff, not just a spectator link. Turn off query-string logging
for the proxied path, or scrub it, on any public-facing proxy.

## Gating desktop mode

The broker cannot tell "an admin configuring emulators" from "anyone who
can call activate" apart; `desktop` is just another `emulator` value on the
same endpoint. If you want that distinction, enforce it upstream:

- In RomM, restrict whichever role or action reaches
  `activate` with `emulator: "desktop"` to accounts you would trust with a
  shell on this host. Do not treat "logged into RomM" as equivalent to
  "trusted with a terminal."
- If RomM's permission model cannot make that split today, do not expose
  desktop mode publicly at all. Run it LAN-only (bind the proxy rule to an
  internal network, or omit desktop from a public-facing config) until it
  can.

## Before you expose this publicly

- [ ] `BROKER_SECRET` is set to a strong random value; `BROKER_DEV_MODE` is
      unset (confirm with the broker's own startup log, it announces both)
- [ ] The container's own port is not reachable from outside its network
      segment; only the reverse proxy and RomM's `broker_host` can reach it
- [ ] The reverse proxy terminates real TLS and is the only public listener
- [ ] The reverse proxy rate-limits or IP-restricts `activate` and `join`
- [ ] The reverse proxy does not log the `token` query parameter for the
      proxied path
- [ ] `ROM_ROOT` and `/config` are the only mounts, and nothing else on the
      host is reachable from this container's network segment
- [ ] The container runs rootless, without `--privileged`, without the
      container runtime's socket mounted in
- [ ] Whoever can reach `emulator: "desktop"` is someone you would hand a
      terminal on this host to directly

## Reporting a vulnerability

Please do not open a public issue for a suspected vulnerability. Use GitHub's
private reporting for this repository (Security tab -> Report a
vulnerability) so it can be assessed before details are public.
