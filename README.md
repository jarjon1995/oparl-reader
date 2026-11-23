# OParl Council Data Reader

Reads any German municipal **OParl 1.0/1.1** endpoint and returns meetings,
agenda items, papers, organisations and people as normalised JSON.

One Actor for roughly a thousand municipalities that already publish OParl,
instead of one scraper per vendor.

## Why this exists

German town councils publish their meetings, agendas and motions in
Ratsinformationssysteme. About a thousand of them expose the open
[OParl](https://oparl.org) standard, so the data is already machine-readable in
principle. In practice it is not, and the reason is not the standard — it is
that every vendor answers differently in ways no client can guess.

This Actor was written after measuring live endpoints from two places: my own
sandbox and the Apify platform. The findings it is built on:

| Measured | Consequence |
|---|---|
| `zv-kisa` answers **HTTP 200 with an OParl `Error` object** on every path | A 200 and parseable JSON is not a System object. The payload's `type` must be checked. |
| `SOMACOS Session` (Köln, Altensteig) answers collections **only under a body**, and **silently ignores `offset`** | Paging by offset re-delivers page one forever. Probe before paging. |
| `SD.NET RIM` (Bochum, Essen, ekom21, kdvz-frechen) **refuses `limit` and `size` with HTTP 400**, but accepts `page=N` | Probing with a parameter the server forbids is itself a bug. Try the bare URL first. |
| `more! Rubin` (westerburg) publishes **singular** collection links (`meeting`, `paper`) on the Body object; the plural path returns an error | Read the links the vendor publishes instead of guessing plural paths. |
| `München Transparent` answers at `/oparl/v1.0` only — `/oparl/system` is a 404 HTML page, and the bare host (no `www`) fails TLS with a certificate hostname mismatch | The entry URL must be the one the server actually serves; a 404 with HTML is not JSON. |
| `ratsinfo-online.net` declares licence *"Nutzung nur nach Genehmigung."* | The licence is read from the server and carried into every output record. |

So the Actor resolves each collection through three routes in order of cost
(top level → the Body object's own published links → body path), verifies each
by fetching it, and then chooses paging mode by probing. The capability
fingerprint of the endpoint is part of the output, not hidden.

## Input

```json
{
  "systemUrl": "https://www.leipzig.sitzung-online.de/oparl/system",
  "collections": ["meetings", "papers"],
  "maxItems": 200,
  "pageSize": 50
}
```

## Output

One dataset item per OParl object: the flattened fields a caller actually
filters on (`id`, `type`, `name`, `start`, `location`, `organizationIds`,
`license`, `sourceSystem`, `vendor`, `fetchedAt`) plus the untouched original
under `raw`. The dataset also carries one `_report` item: which route each
collection resolved through, which paging mode the server proved, the HTTP
status of every route that failed, and the delivery receipt for the item push.
A run that finds nothing still produces a readable report rather than an empty
dataset.

## Verified from the Apify platform, 23 Nov 2026

These runs were executed on the platform, not in a development sandbox, which
matters: outbound reachability differs between the two.

| Endpoint | Vendor | Result | Cost |
|---|---|---|---|
| `westerburg.gremien.info/oparl/system` | more! Rubin | 16 records (8 meetings + 8 papers), agenda items and full-text PDFs intact | $0.0018 |
| `bochum.ratsinfomanagement.net/.../v1.1/system` | SD.NET RIM | 16 records, paging mode `page`, 130 s | $0.0064 |
| `www.leipzig.sitzung-online.de/oparl/system` | Allris | records returned, licence CC-BY-4.0 carried into every item | $0.0011 |
| `www.muenchen-transparent.de/oparl/v1.0` | München Transparent | meetings with full agenda items | ~$0.001 |
| `oparl.mandari.de/oparl/v1/` | mandari (aggregator) | papers with `mandari:originalId` back-links to the source municipality | ~$0.002 |

Reported honestly rather than silently empty:

- `ratsinformation.stadt-koeln.de` (SOMACOS) — answers in 0.1 s from a normal
  connection but **times out from the Apify platform** at 40 s. The endpoint is
  fine; the platform route is blocked or throttled. Do not assume a Köln
  failure here means the Actor is broken.
- `ris-tharandt.zv-kisa.de` — OParl switched off server-side; every path
  returns 200 with an Error object.
- `ratsinfo-online.net` — 403 plus a licence that requires prior permission.

## Licence and manners

Requests are read-only GETs with an identifying User-Agent. The licence each
server declares is copied into every record — some of them say use requires
permission, and that is the server's statement, not this Actor's. Nothing here
is a licence to ignore one.
