# Deployment

How to run SERP Monitor somewhere other than your own laptop: behind an
HTTPS reverse proxy, and with Docker.

[Back to README](../README.md)

## Deploying behind HTTPS

The app serves plain HTTP on `127.0.0.1`. To reach it from other machines,
put a reverse proxy with TLS in front and keep the app on loopback.
[Caddy](https://caddyserver.com) gets and renews a certificate by itself;
the whole `Caddyfile` is:

```
monitor.example.com {
    reverse_proxy 127.0.0.1:8000
}
```

Then start the app with HTTPS enforced and the proxy trusted for
`X-Forwarded-For`/`X-Forwarded-Proto`:

```
FORCE_HTTPS=1 TRUSTED_PROXIES=127.0.0.1 \
  .venv/bin/uvicorn dashboard.main:app --app-dir src \
  --host 127.0.0.1 --port 8000 --proxy-headers --forwarded-allow-ips 127.0.0.1
```

With `FORCE_HTTPS=1`, plain-http requests are redirected to https, HSTS is
sent and every cookie is `Secure` (the session cookie becomes
`__Host-sid`). Run a single worker: sessions and rate limits live in the
process. Never combine a public address with `ALLOW_NO_AUTH`.

## Docker

```
mkdir -p data && docker compose up -d --build
```

Same dashboard on `127.0.0.1:8000`, and sign-in is required here too: the
first visit asks you to create the admin account. `data/` holds the
database, evidence, exports and nightly backups (`data/backups/`, the
directory `make run` uses as well) and
the `.env` that Settings and the setup wizard save keys to; variables in
the repo `.env` are passed to the container and take precedence over it,
even when left empty. Nothing is seeded:
`docker compose run --rm app python scripts/seed_demo_data.py` loads the
demo. The app binds `0.0.0.0` inside the container, so `ALLOW_NO_AUTH` is
refused; behind a reverse proxy, `TRUSTED_PROXIES` is the address the proxy
connects from as the container sees it (the Docker bridge gateway, not
`127.0.0.1`).

Backups live in the volume, so they survive a rebuild and can be copied
off the host from `data/backups/`. To restore one, stop the app first:

```
docker compose stop app
docker compose run --rm app python scripts/restore_backup.py /data/backups/<file>.db --yes
docker compose up -d
```

The variables themselves (`FORCE_HTTPS`, `TRUSTED_PROXIES`, `HOST`,
`ALLOW_NO_AUTH` and the rest) are listed in
[`configuration.md`](configuration.md). The HTTPS items of the security
model are in [`../SECURITY.md`](../SECURITY.md), item 19.
