# Installing matrix-appservice-atproto

Setting the bridge up, natively or in Docker. Because every user's handle is its own hostname, this starts with DNS, a wildcard certificate and nginx before the bridge itself. For what the bridge does, see [README.md](README.md).

## Contents

- [Requirements](#requirements)
- [1. DNS](#1-dns)
- [2. Wildcard TLS certificate](#2-wildcard-tls-certificate)
- [3. nginx](#3-nginx)
- [4. Install and configure](#4-install-and-configure)
- [5. Register the AppService with your homeserver](#5-register-the-appservice-with-your-homeserver)
- [6. Run the bridge](#6-run-the-bridge)
- [7. Verify](#7-verify)
- [Handles](#handles)
  - [Handles under their own subdomain](#handles-under-their-own-subdomain)
- [Things to know before going live](#things-to-know-before-going-live)

## Requirements

- A domain whose **subdomains you control**. ATProto handles are hostnames, so each user's handle is its own subdomain (`alice.example.org`) and has to answer over HTTPS. There is no way around this in the protocol: a handle can't be a path like `example.org/alice`.
- A **wildcard DNS record and wildcard TLS certificate** for that domain (steps 1 and 2 below).
- A reverse proxy (nginx in the examples) in front of the bridge.
- Python 3.12 or newer (developed and run on 3.14), or Docker.

## 1. DNS

Add two things at your DNS provider, both pointing at the server your reverse proxy runs on:

| Record | Type | Value | Purpose |
|---|---|---|---|
| `*.example.org` | `A` (and `AAAA` if you use IPv6) | your server's IP | every user's handle hostname |
| `pds.example.org` | `A`/`AAAA` | your server's IP | the PDS itself (`bridge.public_base_url`) |

The second record is only needed if you want it explicit, or if the PDS lives somewhere other than under the wildcard: `pds.example.org` is already covered by `*.example.org`. Any hostname you control works for the PDS; it doesn't have to be called `pds`.

Existing subdomains are unaffected. An explicit record (`matrix.example.org`, `www.example.org`, ...) always takes priority over the wildcard, and the bridge refuses to hand out a handle that clashes with one (see [Handles](#handles)).

## 2. Wildcard TLS certificate

Let's Encrypt only issues wildcard certificates through the DNS-01 challenge, so the usual HTTP-based certbot setup isn't enough. With a certbot DNS plugin for your provider:

```sh
certbot certonly --dns-<provider> -d '*.example.org' -d 'example.org'
```

If your provider has no plugin or API, [acme-dns](https://github.com/joohoi/acme-dns) works with any provider: you add one `_acme-challenge` CNAME record by hand, once, and renewals are automatic from then on.

## 3. nginx

`deploy/nginx.conf.example` has two server blocks to add to your existing nginx config:

- A **wildcard block** (`server_name *.example.org`) that passes only `/.well-known/atproto-did` and the bridge's own routing check to the bridge. This is what makes handles resolve.
- A **PDS block** (`server_name pds.example.org`) that passes `/xrpc/` (including the long-lived firehose WebSocket relays connect to), `/media/` and `/.well-known/did.json`.

Copy them in, replace `example.org` and the certificate paths, then `nginx -t && systemctl reload nginx`. Three things in that file matter and are easy to lose when adapting it:

- `proxy_set_header Host $host;` is required. The bridge decides whose DID to return from the `Host` header alone.
- The `/xrpc/` location needs the WebSocket upgrade headers and long timeouts, or Bluesky's relay can't stay subscribed and your users' posts never leave the server.
- Nothing under `/_bridge/admin` or `/_matrix/` should be reachable publicly. The example returns 404 for everything it doesn't list.

Your existing server blocks keep working: nginx always prefers a block with an exact `server_name` over the wildcard one.

## 4. Install and configure

**Natively:**

```sh
./scripts/setup.sh
```

This creates a virtualenv, installs `requirements.txt`, and generates `config.yaml` from `config.example.yaml` with fresh random AppService tokens.

**In Docker:** copy `config.example.yaml` to `config.yaml` yourself and replace the two `*_replace_me` tokens (`python3 -c 'import secrets; print(secrets.token_hex(32))'` generates one).

Then edit `config.yaml`. At minimum set:

- `bridge.domain`: the domain handles live under (`example.org`, or a subdomain of it: see [Handles under their own subdomain](#handles-under-their-own-subdomain)).
- `bridge.public_base_url`: the PDS address from step 1 (`https://pds.example.org`). **Choose this before creating any account.** It is written into every account's public identity record.
- `synapse.base_url` and `synapse.server_name`: your homeserver.
- `handles.reserved`: subdomains you use or plan to use, so nobody can claim them as a handle.

`config.example.yaml` documents each of its settings inline (bot name and avatar, command prefix, ghost prefix, space and notifications room names). It is deliberately short: everything else has a default, and [CONFIG.md](CONFIG.md) is the full reference (MSC4501 behaviour, chat, backfill defaults, admins, and more).

## 5. Register the AppService with your homeserver

**Natively:**

```sh
.venv/bin/python -m bridge.appservice config.yaml appservice-registration.yaml
```

**In Docker**, the same module as a one-off run with the entrypoint overridden:

```sh
docker run --rm --entrypoint python \
  -v $(pwd)/config.yaml:/config/config.yaml:ro \
  -v bridge-data:/data \
  ghcr.io/haven-organization/matrix-appservice-atproto:latest \
  -m bridge.appservice /config/config.yaml /data/appservice-registration.yaml
```

That writes the file into the `bridge-data` volume (the only place the container can write). Copy it out to wherever your homeserver can read it:

```sh
docker run --rm --entrypoint cat -v bridge-data:/data \
  ghcr.io/haven-organization/matrix-appservice-atproto:latest \
  /data/appservice-registration.yaml > appservice-registration.yaml
```

The file depends only on `config.yaml`, so it doesn't matter that `docker compose` later names its own volume differently (`<project>_bridge-data`).

Add the resulting file's path to your homeserver's application-service config (`app_service_config_files` in `homeserver.yaml` for Synapse), then restart the homeserver.

## 6. Run the bridge

**Natively:**

```sh
.venv/bin/python main.py
```

Or install `deploy/matrix-appservice-atproto.service` to run it under systemd (see that file for the expected user and paths).

**In Docker:** the `Dockerfile` runs the exact same code. Three settings in `config.yaml` need a container-specific value first:

- `bridge.listen_host` must be `0.0.0.0`, not the native default `127.0.0.1`. Otherwise nothing outside the container can reach it, even with the port published.
- With the default SQLite storage, `storage.data_dir` must be `/data` (not the native default `./data`). That's the volume the image creates and can write to as its non-root user.
- `bridge.internal_base_url` must be an address your homeserver can reach the container at (for example `http://bridge:8091` on a shared Docker network). It is written into the registration file, so set it before step 5.

The container runs as a fixed, non-root `bridge` user, UID/GID `999:999`. A named volume (like `bridge-data` here) needs nothing extra. A bind mount needs the host directory owned by that UID/GID first (`chown -R 999:999 ./data`); on a host with SELinux enforcing, also add the `:z` suffix to the mount.

A prebuilt multi-arch image (amd64/arm64) is published to `ghcr.io/haven-organization/matrix-appservice-atproto` on every push to `main`. Simplest form, standalone:

```sh
docker run -d --name matrix-appservice-atproto \
  -p 127.0.0.1:8091:8091 \
  -v $(pwd)/config.yaml:/config/config.yaml:ro \
  -v bridge-data:/data \
  ghcr.io/haven-organization/matrix-appservice-atproto:latest
```

Or copy `docker-compose.example.yml` to `docker-compose.yml`, adjust it to your setup (it wires in an existing containerized Synapse via an external network; see the file's own comments), then:

```sh
docker compose up -d
```

To build from your own checkout instead of pulling the published image (e.g. to test an unreleased change), `docker build -t matrix-appservice-atproto .` and use that tag in place of the `ghcr.io/...` one above, or swap `docker-compose.example.yml`'s `image:` line for `build: .`.

## 7. Verify

A few seconds after starting, the bridge checks its own wildcard routing through the public internet and logs the result:

```
Wildcard handle routing for *.example.org reaches this bridge
```

A warning there instead means step 1, 2 or 3 isn't right yet, and no handle will be granted until it is. The PDS hostname can be checked by hand:

```sh
curl https://pds.example.org/xrpc/com.atproto.server.describeServer
```

Then, from any Matrix client, invite the bot (`@bluesky:example.org` by default) to a room and send `]create profile`.

## Handles

A user's handle is `<their Matrix username>.<bridge.domain>`. Because that is a real hostname on your domain, three layers decide which names may be handed out, all applied before anyone is granted one:

1. A built-in list of names any site is likely to care about (`www`, `mail`, `admin`, `login`, `api`, `matrix`, ...).
2. `handles.reserved` in `config.yaml`: your site's own subdomains, as exact labels or globs.
3. A live check that the hostname really reaches this bridge and not some other service (`handles.verify_routing`), which catches any subdomain that already exists.

### Handles under their own subdomain

Handles don't have to sit directly under your main domain. `bridge.domain` is separate from the homeserver's name (`synapse.server_name`), so you can put handles one level down and keep them out of the way of your other subdomains entirely:

```yaml
bridge:
  domain: at.example.org        # @alice:example.org -> alice.at.example.org
  public_base_url: https://pds.example.org
synapse:
  server_name: example.org
```

Everything in steps 1 to 3 then applies to that name instead:

- **DNS:** the wildcard record is `*.at.example.org`.
- **Certificate:** it must be for `*.at.example.org`. A `*.example.org` certificate does not cover it, because a wildcard matches exactly one level.
- **nginx:** the wildcard block's `server_name` is `*.at.example.org`.

The PDS address (`bridge.public_base_url`) is unaffected and can stay wherever you like.

Decide this before creating accounts. Changing `bridge.domain` later only affects accounts created afterwards; existing ones keep the handle they have.

## Things to know before going live

- **Creating an account is public and permanent.** Each account gets a DID registered with the public PLC directory. `]delete profile` retires it for good, but the public record that it existed stays.
- **Back up `data/bridge.db`** (or the Postgres database, if you use that backend). It holds the signing keys every account's identity depends on. Posts can be regenerated from Matrix; the keys can't.
