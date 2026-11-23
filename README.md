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

This Actor was written after measuring twelve live endpoints. The findings it
is built on:

| Measured | Consequence |
|---|---|
| `zv-kisa` answers **HTTP 200 with an OParl `Error` object** on every path | A 200 and parseable JSON is not a System object. The payload's `type` must be checked. |
| `SOMACOS Session` (Köln, Altensteig) answers collections **only under a body**, and **silently ignores `offset`** | Paging by offset re-delivers page one forever. Probe before paging. |
| `SD.NET RIM` (Bochum, Essen, ekom21, kdvz-frechen) **refuses `limit` and `size` with HTTP 400**, but accepts `page=N` | Probing with a parameter the server forbids is itself a bug. Try the bare URL first. |
| `more! Rubin` (westerburg) publishes **singular** collection links (`meeting`, `paper`) on the Body object; the plural path returns an error | Read the links the vendor publishes instead of guessing plural paths. |
| `München Transparent` advertises collection links that 404 | An advertised link is not a working one; every route is verified by fetching it. |
| `ratsinfo-online.net` declares licence *"Nutzung nur nach Genehmigung."* | The licence is read from the server and carried into every output record. |

So the Actor resolves each collection through three routes in order of cost
(top level → the Body object's own published links → body path), verifies each
by fetching it, and then chooses paging mode by probing. The capability
fingerprint of the endpoint is part of the output, not hidden.

## Input

```json
{
  "systemUrl": "https://ratsinformation.stadt-koeln.de/oparl/system",
  "collections": ["meetings", "papers"],
  "maxItems": 200,
  "pageSize": 50
}
```

## Output

One dataset item per OParl object: the flattened fields a caller actually
filters on (`id`, `type`, `name`, `start`, `location`, `organizationIds`,
`license`, `sourceSystem`, `vendor`, `fetchedAt`) plus the untouched original
under `raw`. The run's stdout carries the capability report: which route each
collection resolved through, which paging mode the server proved, and the HTTP
status of every route that failed.

## Verified against (23 Nov 2026)

Working: SOMACOS Session (Köln, Altensteig), Allris (Leipzig), more! Rubin
(westerburg), SD.NET RIM (ekom21 Biblis, Essen), München Transparent.
Refused or unreachable, reported honestly rather than silently empty:
zv-kisa (OParl switched off server-side), ratsinfo-online (403 plus a licence
that requires permission), Bochum (WAF rate limit, HTTP 429).

## Licence and manners

Requests are read-only GETs with an identifying User-Agent. The licence each
server declares is copied into every record — some of them say use requires
permission, and that is the server's statement, not this Actor's.
