# Network status of the build environment

Recorded during the environment check required before crawling. These are the
actual probe results, not estimates.

## Egress policy

Outbound HTTPS from this container goes through an agent proxy
(`HTTPS_PROXY=http://127.0.0.1:37607`) that tunnels to a policy-enforcing
egress gateway. The gateway allows a small allowlist and rejects everything
else at the CONNECT stage.

| Host | DNS | Result |
| --- | --- | --- |
| `example.com` | resolves (`2606:4700:10::ac42:93f3`) | `curl: (56) CONNECT tunnel failed, response 403` |
| `x.kiz4dar-zpv.net` | resolves (`84.32.9.233`, `84.32.51.182`) | `curl: (56) CONNECT tunnel failed, response 403` |
| `pypi.org` | resolves | HTTP 200 (allowlisted) |
| `github.com` | resolves | reachable (allowlisted) |

Proxy status endpoint (`$HTTPS_PROXY/__agentproxy/status`) reported:

```json
{ "kind": "connect_rejected",
  "detail": "gateway answered 403 to CONNECT (policy denial or upstream failure)" }
```

DNS resolution works for every host tested, so this is an egress-policy denial
rather than an unreachable origin.

## Consequence

No remote site can be crawled from this environment. Per the operating
instructions, remote requests were stopped after the restriction was confirmed
and development continued offline:

- the crawler is complete and executable;
- a local fixture site exercises every code path (see `fixtures/`);
- the full pipeline — crawl, resume, validate, export, mirror — was run
  end to end against it;
- 228 automated tests pass.

Nothing in this repository reports results from a remote target.

## Running against a real target

Once the environment's network policy permits the target host (configured when
the Claude Code environment is created — see
<https://code.claude.com/docs/en/claude-code-on-the-web>), or when run on a
machine with open egress:

```bash
python run.py crawl https://your-authorized-site.example/ --delay 1.0
python run.py validate
python run.py export
```

The crawler reads `HTTPS_PROXY`/`NO_PROXY` through httpx's standard
environment handling, so it works behind a proxy that permits the target.
