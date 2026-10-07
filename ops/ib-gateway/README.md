# ops/ib-gateway -- the other side of the ports `antlia.gateway` dials

`antlia.gateway` never talks to Docker. It connects to ports on the gateway
host, the same way `antlia.auth` connects to the API on 4001. What is here is
the configuration that has to exist on the *other* side of those ports, kept in
this repo so the gateway project's own files are never edited.

It moved here from pictor (2026-09-14) along with the module that uses it: a
dashboard is not where a broker's infrastructure belongs, and every other
consumer of `antlia.gateway` would have had to reach into pictor for it.

| file | what it does |
|---|---|
| `ib-gateway.override.yml` | one override layer over the gateway project. It reconfigures the `ib-gateway` service -- which **recreates** that container, so a full login -- and adds a `novnc` bridge service, which does not touch it. |
| `ibc-config.ini.tmpl` | the image's IBC template with exactly three lines changed. |
| `check-ibc-template.sh` | says so when the image's template has moved and ours has gone stale. |

**Nothing here is a secret.** The template carries `${...}` placeholders only
(checked), and the override reads every value from the environment. The real
credentials stay in `~/ib-gateway/.env`, outside any repository, where they
cannot be committed by accident.

## The two absolute paths

Both are absolute on purpose, and both have to be updated together if this
directory ever moves:

1. `COMPOSE_FILE` in `~/ib-gateway/.env` names this override.
2. The `volumes:` entry in the override names `ibc-config.ini.tmpl`.

```
COMPOSE_FILE=docker-compose.yml:/home/neroo/repo/antlia/ops/ib-gateway/ib-gateway.override.yml
```

Relative paths in an override resolve against the **project** directory
(`~/ib-gateway`), not against the override's own location -- measured, and it
cost a silently-created directory. Since the override now lives in a different
tree from the project, relative would not merely be fragile, it would be wrong.

That `COMPOSE_FILE` line is doing more than saving typing. Without it a bare
`docker compose up -d` in the gateway directory *drops* the override and
recreates the gateway without the command server, the settings volume, the 2FA
relogin or the noVNC bridge -- silently. It makes the obvious command the
correct one. If this repo moves, compose fails loudly instead of quietly
leaving the layer out.

## Why the template is vendored

`CommandServerPort` is hardcoded to `0` in the image's `config.ini.tmpl`, and
the container generates its real `config.ini` with `envsubst` at every start.
There is no environment variable for it, so the only way in is to supply the
template. Three lines change, each becoming a `${...}` placeholder so this copy
carries no policy of its own -- the values live in the override.

**Mount the template, never the generated `config.ini`.** The generated file is
where `IbLoginId`/`IbPassword` have already been substituted; a copy of *that*
in this repo would be an IBKR password in version control.

**It is CRLF.** Patch it as bytes. Reading it as text normalises the line
endings and every one of its 960 lines then counts as changed, which both hides
the real diff and is a change nobody asked for.

The cost of vendoring is drift: when the image updates its template, ours
silently keeps overriding it, new settings included. `check-ibc-template.sh`
diffs the two and expects exactly the three lines -- run it after pulling a new
image.

## Applying it

Order matters. A recreate means a full login, so the credentials must be good
first -- applying this while they are being rejected only spends more
failed-login attempts against the account.

```bash
cd ~/ib-gateway
docker compose up -d novnc   # the bridge only -- no depends_on, so the gateway stays as it is
docker compose up -d         # everything, incl. the gateway: a login and one 2FA prompt
docker compose stop          # both
```

Then, from anywhere:

```bash
python -m antlia.gateway              # what antlia thinks is on the other side
```

`control: yes` is the override having taken effect. `control: no` means IBC's
command server is not reachable, which is also what a gateway without this
override looks like -- that is a normal answer, not a fault.

## The named volume must be chowned once

Docker creates a named volume owned by **root**; the image runs as
**1000:1000**. The first start after adding `TWS_SETTINGS_PATH` therefore dies
with

    common.sh: /home/ibgateway/settings/jts.ini: Permission denied

and `restart: unless-stopped` turns that into a crash loop. The published ports
go with it, so what you actually see is `ConnectionRefused` on 4001 -- nothing
that mentions permissions. It fails *before* IBC attempts a login, so it costs
no login attempts. Fix once:

```bash
docker run --rm -v ib-gateway_ib-gateway-settings:/s alpine:3.20 chown -R 1000:1000 /s
```

## `.env` values are interpolated, and quoting does not save you

Measured on compose v5.5.0, because this ate hours:

| written in `.env` | what the container receives |
|---|---|
| `PW=ab$cd` | `ab` |
| `PW="ab$cd"` | `ab` -- **double quotes do not protect** |
| `PW=ab$$cd` | `ab$cd` |
| `PW='ab$cd'` | `ab$cd` |

`env_file:` interpolates too -- "move it to `env_file` and the problem goes
away" is false, and was disproved by experiment. Trailing whitespace is eaten
as well.

The clean answer is `TWS_PASSWORD_FILE` plus a compose secret: the entrypoint
reads the file with bash `$(<file)`, which passes through no interpolation at
all and strips only the trailing newline (verified with a 12-character password
containing two `$`).

**`docker compose config` does not print literal values** -- it re-escapes `$`
as `$$`. Comparing its output's length or hash against the real password gives
a confidently wrong answer.

**Shell environment beats the `.env` file.** In a terminal that has run
`set -a; . .env; set +a`, a later `up -d` uses the stale shell values and the
file is ignored.

## `ControlFrom` and `BindAddress`

- **`ControlFrom` must name the docker bridge address** (here `172.18.0.1`).
  IBC's "commands can always be sent from the same host" means the *container*,
  and traffic arriving through a published port is seen coming from the bridge.
  Re-derive it with
  `docker network inspect ib-gateway_default -f '{{(index .IPAM.Config 0).Gateway}}'`
  if the network is ever recreated; the symptom of a stale value is commands
  being refused.
- **`BindAddress` must stay empty.** Blank means "all local addresses"; setting
  `127.0.0.1` binds the container's own loopback, which a published port can
  never reach, and it fails silently.
