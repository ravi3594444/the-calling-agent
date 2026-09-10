# Deploying to Google Cloud

End state: `https://<your-subdomain>.duckdns.org` serves the agent over TLS, so
a phone browser will grant microphone access.

## Why TLS is mandatory

Browsers block `getUserMedia` on insecure origins. `localhost` is exempt, which
is why local development works over plain HTTP — but on a real hostname, **no
certificate means no microphone**, with no useful error beyond a denied
permission. Caddy handles this automatically; don't skip it.

## 1. Get an AssemblyAI API key

<https://www.assemblyai.com/dashboard> → API Keys. This is the only credential
the application needs.

## 2. Create a DuckDNS subdomain

Sign in at <https://www.duckdns.org> with any supported provider. Create a
subdomain (e.g. `my-agent` → `my-agent.duckdns.org`) and copy your **token**
from the top of the page. Leave the IP blank for now.

## 3. Provision the VM

From your laptop, with `gcloud` authenticated:

```bash
gcloud config set project YOUR_PROJECT
./infra/gcp-setup.sh
```

This reserves a static IP, opens only `:80` and `:443`, and creates a Debian 12
`e2-small` in `asia-south1-a` with Docker pre-installed. It prints the IP when
it finishes.

Defaults are overridable:

```bash
ZONE=us-east1-b REGION=us-east1 MACHINE=e2-medium ./infra/gcp-setup.sh
```

### On sizing

The server is a byte relay between two WebSockets — no transcoding, no local
inference. `e2-small` (2 vCPU / 2 GB, ~$13/mo) is ample for the browser path.

Note that `e2-small` and `e2-medium` are **shared-core** machines with burstable
CPU. That is fine for development and light use; if you later put sustained
concurrent traffic through it, move to `e2-standard-2` for dedicated cores.

### On region

Region is close to neutral here. The VM is one hop in a chain, so
`caller → VM → AssemblyAI` sums to roughly the same total wherever the VM sits.
`asia-south1` is the default for lower RTT when testing from an Indian phone.

(This would *not* be neutral if the LLM and TTS were separate services — each
would pay a full round trip per conversational turn, and you would want the VM
next to them. A single persistent WebSocket removes that penalty.)

## 4. Point DuckDNS at the IP

Paste the printed IP into your DuckDNS subdomain's **current ip** field and
click *update ip*. Confirm it resolves before continuing:

```bash
dig +short my-agent.duckdns.org
```

Certificate issuance will fail if this does not yet return your IP.

## 5. Configure the server

```bash
gcloud compute ssh calling-agent --zone asia-south1-a
```

On the VM:

```bash
git clone https://github.com/ravi3594444/the-calling-agent.git
cd the-calling-agent

cp .env.example .env
nano .env      # set ASSEMBLYAI_API_KEY and PUBLIC_HOSTNAME
```

`PUBLIC_HOSTNAME` must be the bare hostname — `my-agent.duckdns.org`, no
scheme, no trailing slash. Caddy uses it as the certificate subject.

Install the DuckDNS updater so the record survives an IP change:

```bash
sudo DUCKDNS_SUBDOMAIN=my-agent DUCKDNS_TOKEN=your-token \
  ./infra/duckdns/install.sh
```

## 6. Start it

```bash
make deploy-up          # docker compose --profile tls up -d
docker compose logs -f
```

Caddy requests a certificate over HTTP-01 on first start; expect a few seconds
before `:443` answers. Watch for `certificate obtained successfully` in the log.

## 7. Test from your phone

Open `https://my-agent.duckdns.org`, tap **Start call**, and allow the
microphone when prompted. You should hear the greeting, and your speech should
appear as live transcript below the button.

## Troubleshooting

| Symptom | Cause |
|---|---|
| Button disabled, "Microphone needs HTTPS" | Page loaded over `http://`. Use the `https://` URL. |
| Permission prompt never appears | Not a secure origin, or previously denied — clear the site's permissions and retry. |
| "AssemblyAI rejected the connection (401)" | Bad or empty `ASSEMBLYAI_API_KEY`. Check `curl localhost:8080/healthz`. |
| "AssemblyAI rejected the connection (404)" | Wrong endpoint path. Flip `ASSEMBLYAI_AGENT_WS_URL` between `/v1/ws` and `/v1/realtime`. |
| Caddy cannot get a certificate | DNS not resolving to this IP yet, or `:80` blocked. HTTP-01 needs both. |
| Agent talks over you | Barge-in relies on `input.speech.started`. Check the browser console for `clear` messages arriving. |
| Agent transcribes its own voice | Speaker bleeding into the mic. Use headphones, or confirm `echoCancellation` is on. |

## Cost

- VM: ~$13/mo for `e2-small`, plus ~$7/mo for the reserved IPv4 address.
- AssemblyAI: **$4.50/hr** of connected time (~$0.075/min), billed while the
  WebSocket is open rather than only while speech is happening.

Stop the VM when idle (`gcloud compute instances stop calling-agent --zone …`).
The static IP is reserved, so the hostname keeps working when you start it again.
