# Managed OmniRoute deployment

The default deployment runs OmniRoute as a service owned by the platform
installation. The end user does not install an OmniRoute plugin or configure
Provider adapters in the Orchestrator codebase.

1. Copy the root `.env.example` to `.env`.
2. Set `OMNIROUTE_IMAGE` to a pinned, compatible OmniRoute image (or use the
   locally built `omniroute:local` image).
3. Start the managed gateway:

```bash
docker compose --env-file .env -f deployment/docker-compose.yml up -d omniroute
```

For an external OmniRoute deployment, do not start the compose service and set
`OMNIROUTE_BASE_URL` to the external service URL. The application still talks
through `OmniRouteAdapter`; the domain contracts and future routing behavior
do not change.

The exact OmniRoute image tag is intentionally an installation decision and
must be pinned by the deployment owner. This prevents the platform from
silently changing gateway behavior when a floating image tag changes.
