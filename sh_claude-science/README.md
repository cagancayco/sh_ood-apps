## `sh_claude-science`

This [Claude Science](https://claude.com/docs/claude-science) OnDemand app starts
a `claude-science` daemon on a compute node and connects the browser to it
through the OnDemand reverse proxy.

### Version

Developed against `claude-science` 0.1.60 (build `8db2454a`, released October
7, 2026), the latest release at the time; see the
[changelog](https://claude.com/docs/claude-science/changelog). The daemon runs
with `--no-auto-update`, so the job never replaces the binary itself: update it
from a shell with `claude-science update` (roll back with
`claude-science update --to <build>`). The job prints the build it starts in the
session log, and warns if it is older than 0.1.60.

### Development install

`claude-science` is still expected in userspace, in `~/.local/bin`, and so is
`socat` (in `~/socat/bin`) for sessions that enable the sandbox.

With the sandbox on, `bwrap` comes from the `system bubblewrap` module, and all
three have to be on `$PATH` before the daemon starts: without `bwrap` the
sandbox preflight refuses to start the daemon, and without `socat` Claude has no
network inside the sandbox. Because the Linux sandbox masks most of `$HOME`, the
two userspace binaries also have to be handed back to it explicitly, which the
job does through a generated `config.toml`:

```toml
[sandbox]
user_read_paths = [ "/home/users/<user>/.local/bin", "/home/users/<user>/socat/bin" ]
```

Once both live on a system path (or ship as a module), that config file and the
`$PATH` line in `template/script.sh.erb` can go away.

### Ports

Three ports are picked with `find_port` at submit time, none of them the
`claude-science` defaults of 8000 and 8001, so that several users can run the app
on the same node:

- `$cs_port` — the web UI, started with `--host 0.0.0.0` and
  `--base-path /rnode/$host/$cs_port`. It also
  needs two `--allow-origin` values, because the daemon gates requests twice:
  the portal origin (`https://ondemand.sherlock.stanford.edu`) clears the CSRF
  check on browser writes and websocket upgrades, and `https://$host:$cs_port`
  clears the `Host` header check — Apache proxies with `Host: <node>:<port>`,
  and the daemon only accepts a `Host` of `localhost` or `127.0.0.1` unless it
  is named here. Without the second one every request answers
  `{"detail":"forbidden host"}`.
- `$cs_sandbox_port` — the separate origin the daemon serves Claude-generated
  HTML previews from. It is proxied like any other port, but the daemon builds
  preview URLs from its own host and port, so previews aren't expected to render
  through OnDemand. Everything else in the UI is unaffected.
- `$port` — the login helper (below). This is the port OnDemand's session card
  points at.

### Why `/rnode`, not `/node`

`--base-path` does not move the daemon's routes: they stay at `/` whatever it is
set to. What it does is scope the login cookie, and set the `<base href>` the
daemon injects into the UI. Every link the UI emits is then *relative* to that —
the sign-in interstitial, for instance, posts to `../../../api/auth/nonce`,
counted from the path the daemon itself was asked for.

That only lands on the right URL if the proxy hands the daemon a root-relative
path, so this app goes behind `/rnode/$host/$port`, which strips the prefix,
alongside `sh_code-server` and `sh_rstudio`. Behind `/node`, which forwards the
whole path, the daemon counts three segments too many and the browser walks up
to the portal root:

```
https://ondemand.sherlock.stanford.edu/api/auth/nonce → 404 Not Found
```

`sh_jupyterlab` sits on `/node` for the opposite reason: Jupyter builds absolute
URLs under its `base_url`, so the prefix has to survive.

### Authentication

`claude-science` signs a browser in with a single-use login link that expires
about three minutes after it is minted, so there is no URL the session card
could hold: by the time anyone clicked it, it would be dead.

`template/bin/cs-login.py` closes that gap. It listens on `$port`, and on each
click asks the daemon for a fresh link — the same thing `claude-science url`
does, over the daemon's control socket — then redirects the browser to that
nonce under `/rnode/$host/$cs_port/`.

The hand-off is a page, not a `Location` header. The helper and the daemon sit
under different `/rnode` prefixes, and Apache rewrites every `Location` coming
out of an `/rnode` backend so that it points back inside that backend's own
prefix. Nothing escapes it:

| `Location` sent | where the browser ends up |
|---|---|
| `/rnode/$host/$cs_port/` | `/rnode/$host/$port/rnode/$host/$cs_port/` |
| `../$cs_port/` | `/rnode/$host/$port/$cs_port/` |
| `https://ondemand…/rnode/$host/$cs_port/` | `/rnode/$host/$port/rnode/$host/$cs_port/` |

Each one arrives back at the helper carrying a nonce and no token, and the
helper answers its own 403. Response bodies, on the other hand, are passed
through untouched — that is how claude-science's own relative links survive — so
the helper returns a small page carrying the login URL in a `<meta refresh>`, a
`location.replace()` and a plain link, and lets the browser resolve it against
the portal page it is already on.

Handing out login links is as good as handing out the session, so the helper
only answers requests carrying `?token=<password>`, where `password` is the
per-session secret generated in `template/before.sh.erb`. The Connect button
posts it; anything else gets a 403.

### Sandbox (off by default)

The app starts `claude-science serve --dangerously-no-sandbox` unless the
"Enable the Claude Science sandbox" box is ticked (but see the organization
policy note below). Without the sandbox, code
Claude writes runs with the user's own permissions and network access, and
no folder or network approval cards are shown: the form says so.

It's off because on Sherlock's compute nodes (glibc 2.17, Linux 3.10) the
0.1.60 sandbox can't run anything, which takes the conda environments, Python
and R cells and the bundled MCP servers (`literature`, `chemistry`, `genomes`,
…) down with it. Three separate problems stack up:

1. **`memfd_create`.** The sandbox writes each mount manifest to a sealed
   memfd, and looks `memfd_create()` up in `libc.so.6`, then `libc.so`.
   glibc 2.17 predates the wrapper (the kernel has the syscall), so every
   sandboxed command fails with `memfd_create is unavailable`. With the sandbox
   on, the job compiles `template/lib/memfd-shim.c` into a `libc.so` and puts
   it on `LD_LIBRARY_PATH`, which gets past this.
2. **`statx`.** Sandboxed commands are then refused with `apply-seccomp:
   refusing to run the command: this sandbox's mounts do not match its plan`.
   `apply-seccomp` (statically linked, shipped in the runtime) checks the mounts
   with `statx`, which the 3.10 kernel lacks (`ENOSYS`, Linux 4.11+), and any
   failure is a refusal. There is no user-space way around this short of
   disabling the check.
3. **Nested network namespaces.** A second level of user namespace fails with
   `ENOSPC` as soon as a network namespace is anywhere in the chain, so the
   nested `bwrap` claude-science uses to run commands inside its own sandbox
   is refused too. Apptainer is no way out: it always runs in a user
   namespace on Sherlock, so even the first level of sandbox fails inside it.

With `--dangerously-no-sandbox`, all of this goes away: on a 0.1.60 test
daemon, the Python (3.11) and R (4.5) skeleton environments and the MCP
environment built, conda binaries run fine on glibc 2.17, and all 25 MCP
servers connected.

**An organization policy overrides this.** If the signed-in Claude account
belongs to an organization that manages Claude Science's network allowlist,
claude-science ignores `--dangerously-no-sandbox` (and `outer_sandbox`) and
starts the sandbox anyway:

```
--dangerously-no-sandbox is not available while your organization manages the network allowlist
```

So the job sets up everything the sandbox needs on every run (the
`bubblewrap` module, `socat`, the `config.toml`, the `memfd_create` shim),
whatever the checkbox says; otherwise the daemon exits at startup with `socat
is required for Linux sandbox networking`. For those accounts the UI and
Claude work, but anything sandboxed hits the `statx` refusal above, so the
conda environments, Python/R cells and bundled MCP servers stay unavailable
until the organization's admin stops managing the allowlist, or a
claude-science release works on a 3.10 kernel. (The no-sandbox test above
ran signed out, so no organization policy applied.)

Fully provisioned, `~/.claude-science` takes about 6GB (environments, runtime
and MCP servers), which lives there whatever the data directory is set to.

### Data directory

State — conversations, credentials, logs, artifacts — lives in
`~/.claude-science` by default, which is shared with any `claude-science` you run
from a login node or another session: only one daemon can run per data
directory, so a second concurrent session needs the "Data directory" form field
pointed somewhere else. That field is also the way to keep a large history off
the 15GB `$HOME` quota, e.g. `$GROUP_HOME/claude-science`.
