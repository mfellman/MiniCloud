# Workflow YAML (in depth)

This document supplements the [README](../README.md) with details on the workflow model, data between steps, and invocation.

---

## 1. File conventions

- **Location**: in containers the orchestrator reads from `WORKFLOWS_DIR` (default `/app/workflows`).
- **Filenames**: `*.yaml` or `*.yml`; the **filename** does not have to match `name:` in the file (but `name` must be unique across all workflows).
- **Loading**: at **startup** the orchestrator loads all valid files. Changes require a **pod restart** (or new deployment) unless you update workflows via volume/ConfigMap externally and restart the pod.

---

## 2. Top-level schema

```yaml
name: <string>                    # required, unique
invocation:                       # optional
  allow_http: <bool>              # default: true
  allow_schedule: <bool>          # default: false
steps:
  - # see §3 onward
```

### 2.1 `name`

- Must match exactly how you start the workflow:
  - URL path: `POST /v1/run/{name}` on the gateway, or `POST /run/{name}` on the orchestrator.
  - JSON body: field `workflow` in `POST /v1/run` and `POST /run`.
- Prefer **ASCII**, letters/digits and optionally `-` or `_`. Special characters in URLs require **URL encoding** on the client.

### 2.2 `invocation`

| Field | Default | Meaning |
|-------|---------|---------|
| `allow_http` | `true` | May be triggered via HTTP: gateway (`/v1/run/...`), orchestrator `POST /run` and `POST /run/{name}`. |
| `allow_schedule` | `false` | May use `POST /invoke/scheduled` on the orchestrator (CronJob, internal worker). |

**Common combinations:**

- Interactive via gateway only: `allow_http: true`, `allow_schedule: false`.
- Batch/cron only: `allow_http: false`, `allow_schedule: true`.
- Both (rare): both `true`.

---

## 3. Step `type: xslt`

```yaml
- id: <string>           # required, unique within the workflow
  type: xslt
  xslt: |                # required: full stylesheet (XSLT 1.0)
    <?xml version="1.0"?>
    <xsl:stylesheet ...>
      ...
    </xsl:stylesheet>
  input_from: <ref>      # optional; see §11
```

- The orchestrator calls **transformers** `POST /applyXSLT`: `xml` = resolved input, `xslt` = the string above.
- **Engine**: XSLT **1.0** (libxslt via lxml). XSLT 2.0/3.0 is not implemented in this stack.

---

## 4. Step `type: http`

```yaml
- id: <string>
  type: http
  http:
    method: <string>           # optional, default GET
    url: <string>              # required, absolute URL
    headers: { <string>: ... } # optional
    body_from: <ref>           # required; see §11
    timeout_seconds: <number>  # optional
```

- The orchestrator calls **egress-http**; it performs the real HTTP request.
- The downstream **response body** (text) is stored under this step’s `id` and becomes **previous** output for following steps.

---

## 5. Step `type: ftp`

```yaml
- id: <string>
  type: ftp
  ftp:
    protocol: <ftp|ftps>        # optional, default ftp
    host: <string>              # required
    port: <number>              # optional, default 21
    username: <string>
    password: <string>
    action: <list|nlst|retrieve|fetch|store|delete>
    remote_path: <string>
    body_from: <ref>            # for action store; see §11
    body_encoding: <utf8|base64>
    timeout_seconds: <number>
```

- The orchestrator calls **egress-ftp** `POST /ftp`.
- Output under this `id` is the service **JSON** response (as a string).

---

## 6. Step `type: ssh`

```yaml
- id: <string>
  type: ssh
  ssh:
    host: <string>
    port: <number>              # optional, default 22
    username: <string>
    password: <string>          # optional if private_key_from
    private_key_from: <ref>     # optional; PEM key
    command: <string>
    timeout_seconds: <number>
```

- The orchestrator calls **egress-ssh** `POST /exec`.
- On **non-zero exit code** the workflow run fails. Output is JSON text (`stdout`, `stderr`, etc.).

---

## 7. Step `type: sftp`

```yaml
- id: <string>
  type: sftp
  sftp:
    host: <string>
    port: <number>
    username: <string>
    password: <string>
    private_key_from: <ref>
    action: <list|retrieve|fetch|store|delete>
    remote_path: <string>
    body_from: <ref>            # for store; see §11
    body_encoding: <utf8|base64>
    timeout_seconds: <number>
```

- The orchestrator calls **egress-ssh** `POST /sftp` (SFTP over SSH).
- For **`retrieve`**, the JSON response includes **`content_base64`** for the file.

---

## 8. Step `type: xml2json`

```yaml
- id: <string>
  type: xml2json
  input_from: <ref>   # optional; source is XML text
```

- Calls **transformers** `POST /xml2json`.
- Output is a **JSON string** (pretty-printed), suitable as input for a **`liquid`** step.

---

## 9. Step `type: json2xml`

```yaml
- id: <string>
  type: json2xml
  input_from: <ref>   # optional; source must be a JSON string (root = object)
```

- Calls **transformers** `POST /json2xml`. The JSON object is turned into XML with **xmltodict.unparse**.

---

## 10. Step `type: liquid`

```yaml
- id: <string>
  type: liquid
  template: |        # Liquid template (python-liquid)
    Hello {{ name }}
  input_from: <ref>   # JSON string as context (object)
```

- Calls **transformers** `POST /applyLiquid` with `template` + `json` (= resolved `input_from`).
- Top-level JSON keys are passed as keyword arguments to `Template.render(**ctx)`; use `{{ foo.bar }}` for nested objects.

---

## 10.1 Workflow context (orchestrator runtime map)

During a run, the orchestrator keeps a **string map** `context` (independent of `outputs` / `previous`). In YAML you can name the storage key **`context_key`** or **`variable`** (same thing). In refs, use **`context:<key>`** or **`var:<key>`** interchangeably.

### `type: context_set`

```yaml
- id: <string>
  type: context_set
  context_key: <string>     # or: variable: <string>
  value: "<literal>"        # exactly one of value, value_from
  value_from: <ref>         # same refs as §11, plus context: / var:
```

- Does **not** change `previous` (pass-through); the step’s `outputs[id]` equals `previous` before the step.

### `type: context_extract_json`

```yaml
- id: <string>
  type: context_extract_json
  context_key: <string>     # or variable:
  input_from: <ref>         # JSON text
  json_path: /path/0/key     # JSON Pointer (RFC 6901), leading /
```

- Parses JSON, reads one value at `json_path`, stringifies (objects/arrays → JSON text), stores under the key, and sets **`previous`** to that string.

### `type: context_extract_xml`

```yaml
- id: <string>
  type: context_extract_xml
  context_key: <string>      # or variable:
  input_from: <ref>          # XML text
  xpath: /root/item/@id      # first node’s string value
```

- Uses **lxml** XPath; first match only. Namespaces: prefer XPath functions such as `local-name()` if needed.

### `type: json_set`

Write a value **into** a JSON document at a **JSON Pointer** path (parent path must already exist). The new value is taken from **`value_from`** (e.g. `var:myLabel` or a literal step). Strings that parse as JSON (objects, arrays, numbers, booleans) are stored as structured JSON; otherwise the raw string is used.

```yaml
- id: <string>
  type: json_set
  input_from: <ref>          # JSON document
  json_path: /a/x            # pointer to the key/index to set
  value_from: <ref>
  mirror_to_context: <key>   # optional: also store full JSON string in context (alias: also_variable)
```

- Sets **`previous`** to the updated JSON text.

### `type: xml_set_text`

Write into the **first** XPath match: either element **text** or an **attribute** (if `attribute` is set).

```yaml
- id: <string>
  type: xml_set_text
  input_from: <ref>
  xpath: /root/item
  value_from: <ref>
  attribute: id              # optional
  mirror_to_context: <key>    # optional (alias: also_variable)
```

`run_workflow` returns the final **`context`** map to the orchestrator (not exposed in the default HTTP response body; it appears in logs/trace).

---

## 10.2 Conditional steps: `when` (IF / CASE-style)

**There are no separate `if` / `case` step types.** Any step may include an optional **`when`** block. If present, the orchestrator evaluates it against **`context`** before running the step. If the condition is **false**, the step is **skipped**: `outputs[id] = previous` (unchanged pipeline), and trace records `skipped: true`, `reason: when`.

```yaml
- id: <string>
  type: liquid               # or any other step type
  when:
    context_key: <string>    # or variable: — read context[context_key]
    equals: "<string>"       # exactly one of: equals, not_equals, one_of
  # not_equals: "<string>"
  # one_of: ["a", "b"]       # CASE-style: value must be one of these
  template: ...
```

- **`equals`** — IF context value equals this string (typical **IF**).
- **`not_equals`** — run if different.
- **`one_of`** — run if context value is in the list (**CASE** arm: use one step per branch with mutually exclusive conditions).

---

## 11. References: `input_from` and `body_from`

`<ref>` is one of:

| Value | Meaning |
|-------|---------|
| `initial` | The **source document** passed to the run (`xml` in the API). |
| `previous` | Output of the **immediately preceding** step (text). |
| `<step_id>` | Stored output of the step with that `id` (must be an **earlier** step). |
| `context:<key>` | The string stored in the workflow **context** under `key` (after a `context_set` / `context_extract_*` step). |
| `var:<key>` | Same as `context:<key>`. |

**Default** when `input_from` is omitted on an **xslt** step:

- First step in the list: as if `initial`.
- Later steps: as if `previous`.

For **http**, `body_from` is **required** explicitly (no implicit default like the first xslt step).

### 11.1 Errors

- Reference to unknown `id` → error during execution (502 with detail in logs).
- HTTP step with non-2xx status → run fails (see orchestrator behavior).

---

## 12. Data flow between steps (conceptual)

```mermaid
flowchart TD
  in[Input XML from client]
  s1[Step 1]
  s2[Step 2]
  out[Final output as HTTP response body]
  in --> s1
  s1 --> s2
  s2 --> out
```

Each step stores a **string** (XML or other text). Only that string is passed via `input_from` / `body_from`.

---

## 12.1 Loops, branching, and “for each item”

The step list stays a **flat list** (no nested YAML blocks). Each step still has one string in `outputs[id]`; **`when`** (§10.2) adds **optional skipping** — not nested subgraphs.

| Need | Approach |
|------|----------|
| IF / CASE on values derived from XML or JSON | **`context_extract_*`** + **`when`** (`equals` / `one_of` / `not_equals`) on subsequent steps. |
| Transform a list inside one payload, one aggregated result | **XSLT 1.0** or **Liquid** inside one step’s string. |
| Many external calls (per item) | One **`http`** batch endpoint, or **multiple workflow invocations** from outside. |
| Nested loops over *steps* | Still not modeled; use XSLT/Liquid, a batch HTTP service, or repeated runs. |

---

## 13. Validation (summary)

- **Unique `id`** per step within one workflow.
- **Unique `name`** per workflow across all loaded files.
- **YAML parse** or pydantic validation errors: file is skipped; see orchestrator logs at startup.

---

## 14. Examples in the repository

| File | Purpose |
|------|---------|
| `services/orchestrator/workflows/minimal.yaml` | Single XSLT step; offline test via `POST /v1/run/minimal`. |
| `services/orchestrator/workflows/demo.yaml` | XSLT + HTTP to external endpoint (needs network). |
| `services/orchestrator/workflows/transform_demo.yaml` | xml2json + Liquid (`Hello, {{ greeting.name }}`). |
| `services/orchestrator/workflows/schedule_only_demo.yaml` | `allow_schedule` only; test `POST /invoke/scheduled`. |

---

## 15. OpenAPI / JSON-schema

HTTP APIs for the gateway and orchestrator are available via FastAPI **OpenAPI** docs on each service (`/docs`); do not expose them publicly in production without authentication.

Back to [README – Workflows](../README.md#workflows-yaml).
