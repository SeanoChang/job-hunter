---
id: E-20260905-4W5W
type: evidence
status: proposed
provenance:
  session: -
  captured_at: 2026-09-05T00:33:55Z
  source: probe
  source_ref: ticket:T-20260904-3MJK
  actor: agent
---

# Oracle CE detail finder pinned: recruitingCEJobRequisitionDetails

Probe result for [[T-20260904-3MJK]]: detail lives on recruitingCEJobRequisitionDetails — recruitingCEJobRequisitions with ById 400s. Winning curl: GET https://jpmc.fa.oraclecloud.com/hcmRestApi/resources/latest/recruitingCEJobRequisitionDetails?onlyData=true&expand=all&finder=ById;Id="210642927",siteNumber=CX_1001 — anonymous 200, byte-identical twice. Fields: Title, ExternalDescriptionStr (full HTML) plus Corporate/OrganizationDescriptionStr, PrimaryLocation, ExternalPostedStartDate. expand=all and siteNumber optional at JPMC; keep both. Re-check per tenant.

## Provenance

> ticket:T-20260904-3MJK
