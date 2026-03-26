# RabbitMQ On Kubernetes

This guide explains how to run your own RabbitMQ broker for MiniCloud workflows and triggers.

## 1. Base resources

MiniCloud includes these manifests:

- `deploy/k8s/rabbitmq-pvc.yaml`
- `deploy/k8s/rabbitmq-deployment.yaml`
- `deploy/k8s/rabbitmq-service.yaml`
- `deploy/k8s/egress-rabbitmq-deployment.yaml`
- `deploy/k8s/egress-rabbitmq-service.yaml`

Apply with Kustomize:

```bash
kubectl apply -k deploy/k8s
```

## 2. Replace default credentials

The default manifests use `guest/guest` for local development only.

Create a secret:

```bash
kubectl create secret generic rabbitmq-auth \
  --from-literal=username='<your-user>' \
  --from-literal=password='<your-password>'
```

Then patch `rabbitmq-deployment.yaml` and `egress-rabbitmq-deployment.yaml` to read from that secret.

## 3. Topic + property conventions

MiniCloud uses RabbitMQ topic pub/sub. Keep these message properties on every event:

- `Domain`
- `Service`
- `Action`
- `Version`

For best interoperability:

- Use exchange `minicloud.events` (type `topic`).
- Use routing key `domain.service.action.version` in lowercase.
- Mirror the same values in message properties so consumers can route on headers/properties if needed.

## 4. Workflow publish configuration

Use a `rabbitmq` connection in `connections/*.yaml` and a `rabbitmq_publish` step in workflows.

Example step:

```yaml
- id: publish_event
  type: rabbitmq_publish
  connection: rabbitmq_events
  rabbitmq:
    message_from: previous
    property_refs:
      Domain: context:domain
      Service: context:service
      Action: context:action
      Version: context:version
```

## 5. Workflow trigger from RabbitMQ

Orchestrator can consume RabbitMQ messages and start workflows (scheduled invocation path).

Set these env vars on the orchestrator deployment:

- `RABBITMQ_TRIGGER_ENABLED=true`
- `RABBITMQ_TRIGGER_URL=amqp://<user>:<password>@rabbitmq:5672/`
- `RABBITMQ_TRIGGER_EXCHANGE=minicloud.events`
- `RABBITMQ_TRIGGER_EXCHANGE_TYPE=topic`
- `RABBITMQ_TRIGGER_QUEUE=orchestrator-trigger`
- `RABBITMQ_TRIGGER_BINDING_KEY=#`

For a dedicated storage event route, add:

- `RABBITMQ_TRIGGER_STORAGE_CHANGED_WORKFLOW=storage_changed_trigger`
- `RABBITMQ_TRIGGER_STORAGE_BUCKET_ALLOW=*` (or `demo,prod-*`)
- `RABBITMQ_TRIGGER_STORAGE_KEY_ALLOW=*` (or `payloads/*`)

Workflow selection priority:

1. Message header `Workflow`
2. `RABBITMQ_TRIGGER_WORKFLOW` (fixed override)
3. Headers/properties `Domain`, `Service`, `Action`, `Version` mapped to workflow name candidates:
   - `domain.service.action.version`
   - `domain-service-action-version`

Important:

- The target workflow must have `invocation.allow_schedule: true`.
- Message body is passed as workflow input payload.

## 5.1 Quick End-to-End Check (storage write -> RabbitMQ -> workflow trigger)

1. Ensure orchestrator trigger env vars are active and workflow `storage_changed_trigger` exists.
2. Write a key through storage API:

```bash
curl -X PUT http://localhost:8086/v1/storage/demo/payloads/last \
  -H "Content-Type: application/json" \
  -d '{"value":"hello","content_type":"text/plain"}'
```

3. Confirm orchestrator consumed and ran a scheduled workflow by checking trace list:

```bash
curl http://localhost:8083/api/traces?limit=20
```

4. Optionally inspect the trace detail for the latest run:

```bash
curl http://localhost:8083/api/traces/<trace_id>
```

## 6. Reliability recommendations

For production:

- Use durable queues and persistent messages.
- Configure dead-letter exchange and retry queues.
- Set queue length/TTL limits to protect the cluster.
- Monitor queue depth and consumer lag.
- Back up RabbitMQ data and test restore.

## 7. Network and exposure

- Keep AMQP (`5672`) and management (`15672`) internal.
- Expose management UI only through secured admin access.
- Use NetworkPolicies so only MiniCloud components can access RabbitMQ.
