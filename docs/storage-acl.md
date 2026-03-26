# Storage ACL and Security

This guide explains how to configure storage access control in a way that is both simple and secure.

The storage service protects key-value endpoints:

- `GET /v1/storage/{bucket}/{key}`
- `PUT /v1/storage/{bucket}/{key}`

ACL checks are role-based and evaluate the caller role(s) from header `X-Storage-Roles`.

## 1. Secure defaults

The service defaults are intentionally restrictive to support production safety:

- `STORAGE_ACL_ENABLED=true`
- `STORAGE_DEFAULT_ROLE=orchestrator`
- `STORAGE_ACL_READ_ROLES=orchestrator`
- `STORAGE_ACL_WRITE_ROLES=orchestrator`

Effect:

- orchestrator can read/write by default.
- other callers are denied unless you explicitly grant roles.

## 2. Configuration modes

Use one of these modes based on complexity.

### Mode A: Simple global roles (recommended start)

Use only two comma-separated lists:

```env
STORAGE_ACL_ENABLED=true
STORAGE_DEFAULT_ROLE=orchestrator
STORAGE_ACL_READ_ROLES=orchestrator,reader
STORAGE_ACL_WRITE_ROLES=orchestrator,writer
```

This applies to all buckets unless overridden.

### Mode B: Per-bucket overrides

Keep simple defaults and override selected buckets:

```env
STORAGE_ACL_ENABLED=true
STORAGE_DEFAULT_ROLE=orchestrator
STORAGE_ACL_READ_ROLES=orchestrator
STORAGE_ACL_WRITE_ROLES=orchestrator
STORAGE_ACL_BUCKET_OVERRIDES={"audit":{"read_roles":["auditor","orchestrator"],"write_roles":["orchestrator"]},"tenant-a":{"read_roles":["tenant-a-reader","orchestrator"],"write_roles":["tenant-a-writer","orchestrator"]}}
```

### Mode C: Full policy object

If you need complete control, set one full JSON policy:

```env
STORAGE_ACL_POLICY={"default":{"read_roles":["orchestrator"],"write_roles":["orchestrator"]},"buckets":{"secure":{"read_roles":["orchestrator","security-reader"],"write_roles":["orchestrator","security-writer"]}}}
```

When `STORAGE_ACL_POLICY` is set, it overrides simple ACL variables.

## 3. Strict mode (deny without explicit role)

To enforce explicit role headers only:

```env
STORAGE_ACL_ENABLED=true
STORAGE_DEFAULT_ROLE=
STORAGE_ACL_READ_ROLES=orchestrator
STORAGE_ACL_WRITE_ROLES=orchestrator
```

With empty `STORAGE_DEFAULT_ROLE`, calls without `X-Storage-Roles` are denied.

## 4. Optional Bearer token layer

You can add token checks on top of ACL:

```env
STORAGE_SERVICE_READ_TOKEN=<secret>
STORAGE_SERVICE_WRITE_TOKEN=<secret>
STORAGE_SERVICE_ADMIN_TOKEN=<secret>
```

This creates two defense layers:

1. Bearer token validation
2. role-based ACL authorization

## 5. Orchestrator integration

The orchestrator forwards roles to storage with `X-Storage-Roles` based on:

- `STORAGE_SERVICE_ROLES` (default: `orchestrator`)

Typical orchestrator setup:

```env
STORAGE_SERVICE_URL=http://storage:8080
STORAGE_SERVICE_ROLES=orchestrator
```

## 6. Environment profiles (copy/paste)

Use these as starting points and tune roles per team.

### Dev profile (fast iteration)

```env
STORAGE_ACL_ENABLED=true
STORAGE_DEFAULT_ROLE=orchestrator
STORAGE_ACL_READ_ROLES=orchestrator,developer
STORAGE_ACL_WRITE_ROLES=orchestrator,developer
STORAGE_SERVICE_ROLES=orchestrator,developer
```

### Stage profile (restricted write)

```env
STORAGE_ACL_ENABLED=true
STORAGE_DEFAULT_ROLE=orchestrator
STORAGE_ACL_READ_ROLES=orchestrator,qa,release-reader
STORAGE_ACL_WRITE_ROLES=orchestrator,release-writer
STORAGE_SERVICE_ROLES=orchestrator
```

### Prod profile (least privilege)

```env
STORAGE_ACL_ENABLED=true
STORAGE_DEFAULT_ROLE=orchestrator
STORAGE_ACL_READ_ROLES=orchestrator,security-reader
STORAGE_ACL_WRITE_ROLES=orchestrator
STORAGE_SERVICE_ROLES=orchestrator
```

Tip: for strict production mode, set `STORAGE_DEFAULT_ROLE=` (empty) so callers must always send explicit roles.

## 7. Troubleshooting

If a call is denied with HTTP 403:

1. confirm ACL mode (`STORAGE_ACL_POLICY` vs simple vars)
2. confirm effective role header (`X-Storage-Roles`)
3. verify bucket override names exactly match bucket values in workflow steps
4. check if `STORAGE_DEFAULT_ROLE` is empty in strict mode

## 8. Workflow scope relation

In OAuth-enabled orchestrator mode, storage steps also require scopes:

- `storage_read` requires `minicloud:storage:read`
- `storage_write` requires `minicloud:storage:write`

This is separate from storage ACL. Both checks can be active.
