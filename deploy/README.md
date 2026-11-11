# Optional VM deployment recipe

Production already serves Charlottesville pools from Krish's Oracle VM,
with Vercel proxying `/api/*`; see [the handoff](../docs/handoff.md).
Its existing service runs from `~/runline` on port 8765. The recipe here uses
`/opt/runline/app` and port 8000, and is intended for a separate deployment,
not an in-place update of that service.

These scripts can host the whole app on a Linux VM, with optional Azure
creation and Blob backup commands below. Serving needs the pool and graph
on local disk. FastAPI already serves `web/`, so one process can answer both
the page and API:

```
browser ──HTTPS──> Caddy :443 ──> uvicorn 127.0.0.1:8000 ──> graphs/ (in the checkout)
                   (certificates)    (runline.service)    └─> /srv/runline/pools/<area>.sqlite
```

| File | What it does | Runs on |
|---|---|---|
| `provision.sh` | Installs everything, and updates it when rerun | VM, as root |
| `runline.service` | systemd unit for uvicorn | VM |
| `Caddyfile` | Reverse proxy with automatic HTTPS | VM |
| `upload_pools.sh` | Checks a pool file, copies it up, swaps it in | Laptop |
| `backup_pools.sh` | Copies pool files to Azure Blob Storage | Laptop |
| `../scripts/pool_smoke.py` | Checks a pool file matches the graph, then that a server answers `source: pool` | Either |

Nothing here has been run against Azure yet. `provision.sh` has run end to
end in a local Ubuntu 24.04 container under systemd (see the end of this
file).

## Metro runs

The service reads metro runs from `/srv/runline/metros`, outside the code
checkout. After measuring the actual DC tile graphs with
`scripts/measure_tile_graphs.py`, copy each complete run into its own
directory there, containing `pools.sqlite` and `tile-graphs/<tile>.pkl.gz`.
For an old merged file, add its cell/tile index using the matching plan and
`scripts/merge_pools.py --add-index` before uploading. Publish the completed
directory with a rename on the same filesystem so requests never see a
partial upload. The `runline` service user needs read access to all files.

`upload_pools.sh` handles manifest areas such as Charlottesville; metro runs
need their tile graphs as well as the SQLite file. The B1s measurements below
cover Charlottesville only. Measure DC before selecting its VM size or graph
cache size; no actual DC tile graphs were available during local integration.

## Sizing: one worker on a B1s

Measured on the dev laptop (macOS, Python 3.13, other jobs running) with a
57-pool sample of Charlottesville (25 cells spread across the area, 8 of them
pooled on `mixed` only) and uvicorn running this checkout. RSS is one
worker's peak, sampled every 30 ms.

| Worker state | RSS |
|---|---|
| Started, nothing loaded | 66 MB |
| After its first request (walk graph loaded) | 250 MB |
| 36 pool requests, 3/8/12 mi across 12 cells | 250-257 MB |
| One live request on a fresh worker: 3 / 8 / 12 mi | 268 / 376 / 428 MB |
| Live 3, 8 then 12 mi | 497 MB |
| Many live 12 mi requests (prepared-graph cache full) | 590-620 MB |
| Then 12 mi drive-radius requests, three of them at once | 775 MB |
| Two workers, idle: supervisor and spawn helper | +44 MB |

| Request | Warm latency (median, range) |
|---|---|
| Pool, 3 / 8 / 12 mi | 17 / 24 / 34 ms (11-43 ms) |
| First request on a cold worker (graph load) | 400-490 ms |
| Live, 3 / 8 / 12 mi | 0.11 / 1.7 / 2.2 s (0.03-2.9 s) |
| Live with a 5 mile drive radius, 8-12 mi | 7-8 s |
| Uncovered start, downloads off | 422 in 3 ms |

Linux agrees. The same worst-case sequence against the service as
`provision.sh` installs it, on Ubuntu 24.04 in a local container (arm64,
Python 3.12), used 207 MB of anonymous memory once the graph loaded, about
530 MB with the cache full and 736 MB at worst (757 MB RSS). Rerun with the
limits a B1s would get (`MemoryHigh=564M`, `MemoryMax=764M`), it finished
with no OOM kill or restart, 186 MB swapped out and latencies within about
10%; the container's swap is faster than an Azure disk, so expect more there.

The full 110 MB Charlottesville pool changes the disk numbers, not the memory
ones. Serving opens the SQLite file per request, reads one 8-9 KB row and
closes it, so pool size adds nothing to a worker; at most 110 MB of
reclaimable, shared page cache. Disk: the sample was 10.4 KB per pool, which
predicts 136 MB for 13,092 pools against the real 110 MB; with the checkout
and the virtualenv the VM needs well under 1 GB. The sample answered 39% of
requests from pools against 71% in `compare_pools.py`'s 40 starts, because
its cells include the rural edge; the full file should send fewer requests
live, and memory is set by the live ones.

What that means for the VM:

- **Each worker holds its own graph**, so memory scales with workers: about
  600 MB each in steady use, up to roughly 800 MB at worst.
- **B1s (1 vCPU, 1 GB) gets one worker.** A second can't help on one vCPU,
  because live planning is CPU-bound Python and two processes would only take
  turns, and it would need another 600-800 MB. One worker still answers
  concurrently: requests run on a thread pool, and three 12 mile requests at
  once finished in 3.7-5.2 s against 2.2 s alone, with no errors.
- **Swap carries the worst case.** 775 MB plus Ubuntu and Caddy is more than
  1 GB, so `provision.sh` adds a 2 GB swap file, and the unit's `MemoryHigh`
  makes the kernel swap RunLine's cold pages (old cached crops) rather than
  sshd's. A burst of drive-radius requests gets slower; it shouldn't fail.
- **CPU credits.** A B1s runs at 10% of a vCPU baseline and banks credits
  when idle, about 6 minutes of full CPU per hour. At the laptop's 2.2 s per
  live 12 mile request that is 100-150 an hour indefinitely, more in bursts,
  and pool answers cost almost nothing. Expect live requests to run slower
  than on the laptop.
- **When to move up.** If `journalctl -k` shows OOM kills, or live answers
  stay slow, resize to a B1ms (2 GB, still 1 worker) or a B2s (4 GB, 2
  vCPUs: `provision.sh` picks 2 workers). Both cost money that a B1s doesn't,
  and a B2s at about $30 a month would use the $100 credit in about three
  months. Metros (handoff task 2) need their tile graphs measured first.

`provision.sh` picks the worker count from the VM's RAM: `(MB - 500) / 800`,
at least 1 and at most one per vCPU. It sets `MemoryHigh` to RAM minus 350 MB
and `MemoryMax` to RAM minus 150 MB. Rerun it after a resize.

## What the app needs off Vercel

- `RUNLINE_ALLOW_OSM_DOWNLOAD=0`. Without `VERCEL` set, downloads used to
  count as allowed, so a start outside the shipped map would try osmnx, which
  `requirements.txt` doesn't install: a 500 instead of the coverage message
  (or, with osmnx present, an Overpass download inside a request). The unit
  sets the variable, and `downloads_allowed()` now also says no when osmnx
  isn't installed.
- `RUNLINE_CACHE_DIR=/var/cache/runline`. `cache_root()` would try the
  checkout's `cache/`, which the service user can't write, and fall back to a
  shared `/tmp`. The unit sets the variable and systemd creates the directory.
  With baked elevation and downloads off it is barely used.
- `RUNLINE_POOLS_DIR=/srv/runline/pools`, outside the checkout, so updating
  the code never touches the pools.
- uvicorn trusts `X-Forwarded-*` only from Caddy on 127.0.0.1.

## Runbook

Run these from the repo root on your laptop. Nothing below has been run yet.

### 0. Before you start

- The [Azure CLI](https://learn.microsoft.com/cli/azure/install-azure-cli),
  and an Azure for Students subscription.
- The 110 MB `charlottesville-va.sqlite` from Krish's Drive folder, saved as
  `pools/charlottesville-va.sqlite`. Check it first; serving ignores a pool
  built from a different graph and plans everything live:

  ```bash
  .venv/bin/python scripts/pool_smoke.py --pool pools/charlottesville-va.sqlite
  ```

- This branch on GitHub. The VM clones the repo, so push `host-pools-vm` (or
  merge it) before step 2.

### 1. Create the VM

```bash
az login
az account set --subscription "Azure for Students"
az account show --query "{name: name, state: state}" -o table

LOCATION=eastus
RG=runline-rg
VM=runline-vm
# Pick once and keep it: the VM's name becomes $DNS.$LOCATION.cloudapp.azure.com.
DNS=runline-$RANDOM
echo "$DNS"

# Skip if you already have a key you want to use. Azure takes ed25519 keys.
ssh-keygen -t ed25519 -f ~/.ssh/runline_azure -C runline-azure

az group create --name $RG --location $LOCATION

# Standard_B1s is in the 12-month free services (750 hours a month), and a
# 64 GB Premium SSD is the free tier's P6 disk size. --authentication-type ssh
# turns password login off.
az vm create \
  --resource-group $RG --name $VM \
  --image Canonical:ubuntu-24_04-lts:server:latest \
  --size Standard_B1s \
  --admin-username azureuser \
  --authentication-type ssh \
  --ssh-key-values ~/.ssh/runline_azure.pub \
  --public-ip-sku Standard \
  --public-ip-address-allocation static \
  --public-ip-address-dns-name $DNS \
  --storage-sku Premium_LRS --os-disk-size-gb 64 \
  --nsg-rule SSH

az vm open-port --resource-group $RG --name $VM --port 80 --priority 1010
az vm open-port --resource-group $RG --name $VM --port 443 --priority 1020

# Optional but worth it: only your current IP can SSH in. Rerun when your IP
# changes (UVA Wi-Fi and home differ), or SSH will time out.
az network nsg rule update --resource-group $RG --nsg-name ${VM}NSG \
  --name default-allow-ssh --source-address-prefixes "$(curl -s https://api.ipify.org)/32"

az vm show -d --resource-group $RG --name $VM --query "{ip: publicIps, fqdn: fqdns}" -o table
```

If `az vm create` fails with `SkuNotAvailable` or a policy error naming the
region, Azure for Students limits regions per account: try `eastus2` or
`centralus` (`az vm list-skus -l <region> --size Standard_B1s -o table`).

Add the VM to `~/.ssh/config` so the scripts below can say `runline`:

```
Host runline
  HostName <fqdn from above>
  User azureuser
  IdentityFile ~/.ssh/runline_azure
```

**Spending.** Azure for Students has a spending limit: when the $100 credit
runs out, Azure stops the resources instead of billing a card. Expected cost
is near zero: the B1s and its disk are free-tier, 100 GB a month of outbound
traffic is free, and the static public IPv4 is about $3.65 a month if the
free services don't cover it. Check Cost Management > Cost analysis in the
portal after the first day, and set a budget alert there (say $5 a month).

### 2. Provision

```bash
scp deploy/provision.sh runline:
ssh runline sudo bash provision.sh --domain <fqdn> --ref host-pools-vm
```

Drop `--ref` once this branch is merged (it defaults to `main`). It ends by
checking uvicorn answers. Then open `https://<fqdn>`: Caddy gets the
certificate on the first request, which can take a few seconds. If Let's
Encrypt refuses the shared `cloudapp.azure.com` name, Caddy falls back to
ZeroSSL by itself.

### 3. Upload the pool and check it serves

```bash
deploy/upload_pools.sh runline pools/charlottesville-va.sqlite
.venv/bin/python scripts/pool_smoke.py --pool pools/charlottesville-va.sqlite \
    --api https://<fqdn> --cells 5
```

`pool_smoke.py` reads its starts from the pool file, asks for 3, 8 and 12
miles from each, and exits 1 if nothing came back `source: pool`. That is the
handoff's definition of done. Some `live` answers are normal: a pooled cell
still plans live when too few loops fall in the distance band.

### 4. Back the pools up to Blob Storage

Once:

```bash
BACKUP_RG=runline-backup-rg   # separate, so deleting the VM's group keeps the backups
ACCOUNT=runlinepools$RANDOM   # 3-24 lowercase letters and digits, globally unique; keep it
az group create --name $BACKUP_RG --location $LOCATION
az storage account create --resource-group $BACKUP_RG --name $ACCOUNT --location $LOCATION \
  --sku Standard_LRS --kind StorageV2 --min-tls-version TLS1_2 --allow-blob-public-access false
az role assignment create --role "Storage Blob Data Contributor" \
  --assignee "$(az ad signed-in-user show --query id -o tsv)" \
  --scope "$(az storage account show --resource-group $BACKUP_RG --name $ACCOUNT --query id -o tsv)"
```

The role can take a few minutes to apply. Then, and after any new pool build:

```bash
deploy/backup_pools.sh $ACCOUNT pools/charlottesville-va.sqlite
```

110 MB of LRS storage costs well under a cent a month. To restore, find the
backup whose `graph` matches the graph you run, download it, and upload it as
in step 3:

```bash
az storage blob list --account-name $ACCOUNT --container-name pools --auth-mode login \
  --prefix charlottesville-va/ --include m --query "[].{name: name, graph: metadata.graph}" -o table
az storage blob download --account-name $ACCOUNT --container-name pools --auth-mode login \
  --name charlottesville-va/<hash>.sqlite --file pools/charlottesville-va.sqlite
```

### 5. Day to day

| Task | Command |
|---|---|
| Update the code | Step 2 again, with the same flags |
| Logs | `ssh runline journalctl -u runline -f` (Caddy: `-u caddy`) |
| Memory | `ssh runline systemctl status runline` (the Memory line), `free -m`, `journalctl -k \| grep -i oom` |
| New pool file | `deploy/upload_pools.sh runline pools/<area>.sqlite`; no restart needed |
| Resize | `az vm resize --resource-group $RG --name $VM --size Standard_B2s`, then rerun `provision.sh` |
| Pause | `az vm deallocate --resource-group $RG --name $VM` (`az vm start` to resume; the IP is kept) |
| Remove | `az group delete --name $RG` (the backups survive in `runline-backup-rg`) |

The first request after a restart takes about half a second longer while the
worker loads the graph.

## Vercel: move the site, or proxy to it

Two ways to put the VM behind the app people visit:

- **(a) Move the whole site to the VM** and point the domain there. One
  origin, one deploy, the page and the API always the same version, and no
  CORS. Vercel's deployment keeps working as before, live only, which makes it
  a rollback if the VM is down.
- **(b) Keep Vercel for the page and send `/api` to the VM.** Either FastAPI
  gains CORS for the Vercel origin, or `vercel.json` gets an external rewrite
  ahead of its catch-all (`{"src": "/api/(.*)", "dest": "https://<fqdn>/api/$1"}`),
  which Vercel proxies so the browser sees one origin. Either way there are
  two deployments to keep in step, every request takes an extra hop, and the
  VM still needs HTTPS.

**Recommendation: (a).** It's what FastAPI serving `web/` was built for, it
removes a moving part instead of adding one, and (b) buys nothing the VM
can't already do: the page is a few static files. The domain move is yours to
make. To put a custom domain on the VM, add an A record for it pointing at
the VM's IP, then rerun `provision.sh` with both names,
`--domain <fqdn>,<your domain>`, so Caddy serves and certifies both. Whether
`runline-nine.vercel.app` redirects to it (a `redirects` entry in
`vercel.json`) or stays up as the live-only fallback is also your call.

## What was tested without a VM

- `provision.sh` in an Ubuntu 24.04 container booted with systemd, cloning
  this branch from a git bundle, with `--domain localhost --swap-gb 0`. It ran
  twice cleanly (the second time as an update), the unit started as
  `runline` with its environment, and Caddy served `https://localhost` with
  its internal CA, redirected port 80, gzipped JSON and set the headers.
- The remote half of `upload_pools.sh`, verbatim, as the uploader in that
  container: checksum, graph check, rename. Then `pool_smoke.py` inside it
  against the service: pooled cells answered `source: pool` in 10-41 ms
  warm, and the same starts on an unpooled surface answered `live`.
- `pool_smoke.py` rejects a pool whose graph hash doesn't match.
- Not tested: Azure itself, x86_64, a real certificate from Let's Encrypt,
  making the swap file, and `backup_pools.sh` (it needs `az`). Step 2 is the
  first real run, so read its output.
