# User guide: writing orchestrations (workflows)

This guide is for **people who write workflow YAML** — integration developers, not cluster operators. It explains how to model a pipeline, pick step types, and pass data between steps.

For the full field-by-field reference, see **[Workflow YAML (in depth)](workflows.md)**. For HTTP URLs and deployment, see the [README](../README.md).

---

## 1. What you are writing

An **orchestration** is a **linear list of steps** in one YAML file. Each step:

- Has a unique **`id`** within that file.
- Produces **one text output** (XML, JSON text, plain text, …) that flows to the next step unless you point elsewhere.
- Uses **`type`** to choose a building block (transform, HTTP call, FTP, …).

The client sends **one input document** (`xml` in the API — often XML, but it can be JSON text if your first steps expect that). The **last step’s output** is returned as the HTTP response body.

```mermaid
flowchart LR
  client[Client POST body]
  s1[Step 1]
  s2[Step 2]
  sN[Step N]
  out[Response body]
  client --> s1 --> s2 --> sN --> out
```

There is **no nested graph** in YAML: no `if` blocks or `for` loops as separate constructs. You can still branch with **`when`** (see §6) and repeat logic **inside** XSLT or Liquid strings.

---

## 2. Minimal workflow

```yaml
name: hello
steps:
  - id: wrap
    type: xslt
    xslt: |
      <?xml version="1.0" encoding="UTF-8"?>
      <xsl:stylesheet version="1.0" xmlns:xsl="http://www.w3.org/1999/XSL/Transform">
        <xsl:template match="/">
          <out><xsl:copy-of select="."/></out>
        </xsl:template>
      </xsl:stylesheet>
```

- **`name`** must match the URL: `POST .../v1/run/hello` (gateway) or `POST .../run/hello` (orchestrator).
- **`id`** is required for every step and must be unique.

**Try it** (local Compose, gateway on port 8080):

```bash
curl -s -X POST http://127.0.0.1:8080/v1/run/hello \
  -H 'Content-Type: application/json' \
  -d '{"xml":"<?xml version=\"1.0\"?><doc/>"}'
```

If your environment uses **`HTTP_INVOCATION_TOKEN`**, add:

```text
-H 'Authorization: Bearer <your-token>'
```

---

## 3. How data moves: `initial`, `previous`, and step ids

| Concept | Meaning |
|--------|---------|
| **`initial`** | The document the client sent in the request body (`xml` field). |
| **`previous`** | The output of the step **immediately before** this one. |
| **`<step_id>`** | The stored output of an **earlier** step with that `id`. |

Most steps use **`input_from`** or (for HTTP) **`body_from`** with one of:

- `initial`
- `previous`
- `context:myKey` or **`var:myKey`** — a value stored in the **runtime context** (see §5)
- another step’s **`id`**

If you omit **`input_from`** on **`xslt`**:

- First step defaults to **`initial`**.
- Later steps default to **`previous`**.

**HTTP steps** always require an explicit **`body_from`** (no implicit default).

---

## 4. Step types at a glance

| `type` | Use when you need to … |
|--------|-------------------------|
| **`xslt`** | Transform XML with **XSLT 1.0**. |
| **`xml2json`** | Turn XML → JSON text. |
| **`json2xml`** | Turn JSON text → XML. |
| **`liquid`** | Render a **Liquid** template with a JSON object as context. |
| **`http`** | Call an **external HTTP** URL (via egress). |
| **`ftp`**, **`sftp`** | List/get/put files over **FTP/FTPS** or **SFTP**. |
| **`ssh`** | Run a **shell command** over SSH. |
| **`context_set`** | Store a string under a **variable** (context key) for later steps. |
| **`context_extract_json`** | Read one value from JSON with a **JSON Pointer** (`/a/0/b`). |
| **`context_extract_xml`** | Read text with **XPath** from XML. |
| **`json_set`** | Write a value **into** a JSON document at a path. |
| **`xml_set_text`** | Set **element text** or an **attribute** on the first XPath match. |

Details and all fields: **[workflows.md](workflows.md)**.

---

## 5. Variables (context map)

The orchestrator keeps a **map of named strings** (your **variables**) for the duration of one run.

- **Write:** `context_set` with **`context_key`** or **`variable`** (same thing), plus either **`value`** or **`value_from`**.
- **Read in refs:** `context:myKey` or **`var:myKey`**.
- **Populate from JSON/XML:** `context_extract_json` / `context_extract_xml` put the result into a variable **and** set **`previous`** to that string (so the next step can consume it easily).

Use variables when:

- You need a value in **`when`** (see §6).
- You need the same value in **`json_set`** / **`xml_set_text`** **`value_from`** without repeating a long pipeline.

---

## 6. Conditional steps (`when`) — IF / CASE

There is no separate `if` step. Any step may include **`when`:**

```yaml
when:
  context_key: status    # or: variable: status
  equals: "ok"
# or: not_equals: "failed"
# or: one_of: ["a", "b", "c"]
```

If the condition is **false**, the step is **skipped** (the pipeline output stays as **`previous`**; trace shows `skipped`).

Typical **CASE** pattern: several steps, each with **`when.one_of`** or different **`equals`** on the same variable, so exactly one branch runs.

---

## 7. Invocation: who may start this workflow?

```yaml
invocation:
  allow_http: true      # default; gateway / POST /run
  allow_schedule: false # default; internal POST /invoke/scheduled
```

- **`allow_http: false`** — cannot use public/gateway URL triggers; use scheduled (or internal) only.
- **`allow_schedule: true`** — allows **`POST /invoke/scheduled`** (e.g. CronJob).

Token requirements are **not** in YAML: operators configure either **shared bearer secrets** or **OAuth2 JWT + scopes** on the orchestrator. See [README – Invocation](../README.md#optional-bearer-tokens-vs-oauth2-on-the-orchestrator) and **[OAuth2 / scopes](oauth-authorization.md)** — scopes can limit **workflow names** and **egress** (outbound HTTP, FTP, SSH) per client.

---

## 8. Practical tips

1. **Keep `id` short and stable** — they are used in refs and error messages.
2. **Start small** — one `xslt` or one `http` step, then add steps.
3. **Prefer explicit `input_from`** when the pipeline is non-obvious (so the next reader sees the data source).
4. **Large transforms** — XSLT and Liquid can loop inside **one** step; you do not need YAML loops.
5. **Deploying YAML** — in Kubernetes, workflow files are usually a ConfigMap or mounted volume; **changing YAML requires reloading** the orchestrator (restart rollout). Coordinate with ops; see [Kubernetes (in depth)](kubernetes.md).

---

## 9. Examples in this repository

| File | Idea |
|------|------|
| `workflows/minimal.yaml` | Single XSLT; good first test. |
| `workflows/demo.yaml` | XSLT then HTTP. |
| `workflows/transform_demo.yaml` | xml2json + Liquid. |
| `workflows/schedule_only_demo.yaml` | Scheduled-only (`allow_http: false`). |

---

## 10. Where to go next

| Document | Content |
|----------|---------|
| **[workflows.md](workflows.md)** | Full schema, every step type, JSON Pointer, XPath, errors. |
| **[README – Workflows](../README.md#workflows-yaml)** | Summary tables and curl examples. |
| **[README – Invocation](../README.md#invocation-http-vs-scheduled)** | HTTP vs scheduled, Bearer tokens. |

Back to [README](../README.md).
