# Enterprise-grade beveiliging (richtlijnen en backlog)

Dit document vat **bevindingen en aanbevelingen** samen om MiniCloud richting *enterprise-grade* security te brengen. Het is geen certificering; gebruik het als **checklist** en prioritering samen met jullie risico- en compliance-eisen.

---

## 1. Identiteit en autorisatie

| Thema | Aanbeveling |
|--------|-------------|
| Token lifecycle | Korte access tokens, refresh buiten de kritieke pad, **rotatie** van signing keys (JWKS), duidelijke **audience**- en **issuer**-validatie overal waar JWT’s worden geaccepteerd. |
| Scope-model | Naast `minicloud:egress:*` en optionele **connection-scopes**: periodiek **review** in het IdP welke clients welke **workflows** en **connections** mogen combineren. |
| Service-to-service | Gateway → orchestrator vertrouwt nu op netwerk + beleid; overweeg **mTLS** of workload identity (bijv. SPIFFE/SPIRE of cloud-native workload IAM) zodat alleen de gateway (of goedgekeurde callers) workflows start. |

Zie ook [OAuth2 / scopes](oauth-authorization.md).

---

## 2. Geheimen en configuratie (workflows en connections)

| Thema | Aanbeveling |
|--------|-------------|
| Geen secrets in git | Wachtwoorden, PEM-keys en API-keys **niet** in gewone ConfigMaps of YAML in het repository; gebruik **Kubernetes Secrets**, **External Secrets Operator**, HashiCorp Vault of cloud secret stores. |
| Scheiding van rollen | RBAC: wie **workflows** deployt vs wie **connection-secrets** beheert (GitLab + Kubernetes). |
| Key material | SSH/SFTP: voorkeur voor **gemounte secrets** of secret refs boven inline PEM in YAML (zie ook connection-documentatie in [workflows.md](workflows.md)). |

---

## 3. Egress en netwerk (hoog risico bij HTTP-call)

| Thema | Aanbeveling |
|--------|-------------|
| SSRF | **HTTP egress**: beperk bestemmingen met een allowlist. De egress-http service ondersteunt **`HTTP_EGRESS_ALLOWED_HOSTS`** (komma-gescheiden hostnamen). Blokkeer ook cloud metadata (`169.254.169.254`) en interne ranges via beleid/netwerk waar mogelijk. |
| Segmentatie | **NetworkPolicies**: alleen gateway → orchestrator; alleen orchestrator → transformers en egress; geen rechtstreeks internet voor pods die dat niet nodig hebben. |
| Andere egress | FTP/SSH/SFTP: strikte timeouts, limieten op response-grootte (HTTP egress heeft o.a. `HTTP_EGRESS_MAX_RESPONSE_BYTES`), geen willekeurige commando’s zonder beleid als functionaliteit uitbreidt. |

---

## 4. Transport en rand (perimeter)

- **TLS** voor alle client-facing endpoints (Ingress → gateway); overweeg **HSTS** waar van toepassing.
- **Rate limiting** / **WAF** vóór de gateway tegen misbruik van o.a. `/v1/run` en credential stuffing (ook bij OAuth blijft dit relevant).
- **Request size limits** en timeouts op gateway en orchestrator om resource-uitputting te beperken (deels al configureerbaar; productie-afspraken vastleggen).

---

## 5. Observability en compliance

- **Audit trail**: wie welke workflow wanneer startte (subject uit JWT), correlatie met `X-Request-ID`; **geen** volledige payloads in logs tenzij beleid dat expliciet toestaat.
- **Integriteit**: export naar SIEM; waar vereist: onveranderbare logretentie (WORM) voor forensics.

---

## 6. Platform en supply chain

- **Container images**: non-root, waar mogelijk read-only root filesystem, minimale base images, regelmatige **CVE-scans** (Trivy, Grype) in CI.
- **Dependencies**: automatisering (Dependabot/Renovate), SBOM (bijv. Syft), **image signing** (cosign) voor productie-artefacten.
- **GitLab**: branch protection, verplichte reviews, geen secrets in logs/artifacts; masked variables.

---

## 7. Proces en assurance

- **Threat modeling** (bijv. STRIDE) gericht op orchestration + egress + tokenmisbruik.
- **Penetration test** / red team: gateway, OAuth-flows, SSRF via egress-HTTP, privilege escalation via scopes.
- **Incident response**: runbooks, secret rotation, contactpunten.

---

## 8. Snelle prioritering (praktisch)

1. **Secrets** uit repo en gewone ConfigMaps; connection-credentials alleen via Secret stores.  
2. **SSRF**: `HTTP_EGRESS_ALLOWED_HOSTS` afdwingen in elke omgeving waar workflows externe URL’s aanroepen.  
3. **NetworkPolicies** + gestructureerde **audit logging** (JWT-subject, workflow- en connection-naam).  
4. Daarna: **S2S-hardening** (mTLS/workload identity) en **randbeveiliging** (rate limit / WAF).

---

## Gerelateerde documentatie

- [OAuth-authorization](oauth-authorization.md)  
- [Workflows (YAML)](workflows.md)  
- [Kubernetes](kubernetes.md)  
- [README § Security and production](../README.md#security-and-production)
