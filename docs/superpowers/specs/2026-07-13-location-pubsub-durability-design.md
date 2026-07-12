# Durable Location Processing with Pub/Sub

## Goal

Replace the production API's in-memory location-processing fallback with the repository's existing Pub/Sub push-worker architecture, while keeping every Cloud Run service on request-based billing and allowing scale-to-zero.

## Scope

This change covers only infrastructure durability for the existing location task types:

- `pipeline`
- `details_enrich`
- `emoji`
- `photos`
- `menu_vibe`
- `vibe_reprocess`

It does not change which tasks the application dispatches, location completeness rules, expanded-card hydration, Maps URL behavior, or Magic Search response fields. Those remain separate follow-up items.

## Constraints

- Work directly on `main`, preserving unrelated changes.
- Deploy to Google Cloud project `pinit-494520` in `europe-west2` only after local validation.
- Keep the API, fast worker, and menu worker on request-based Cloud Run billing.
- Explicitly deploy all three services with `--cpu-throttling` and `--min-instances 0`.
- Do not use `--no-cpu-throttling`, always-on CPU, permanent instances, or a worker pool.
- Keep the existing Pub/Sub topic, task payloads, task filters, ordering keys, and worker HTTP endpoint.
- Make provisioning safe to rerun.

## Recommended Architecture

The API publishes one message to `location-tasks` with `task_type`, `request_id`, `location_id`, and `source` attributes. Six filtered push subscriptions route each task type to one of two private Cloud Run services:

- `pipeline`, `details_enrich`, `emoji`, `photos`, and `vibe_reprocess` go to `pinit-location-worker`.
- `menu_vibe` goes to the larger `pinit-location-worker-menu` service.

Workers return a success status only after `handle_location_task` finishes. A failure returns HTTP 500, so Pub/Sub retries the same message. After ten delivery attempts, Pub/Sub forwards the message to `location-tasks-dead-letter`. A retained pull subscription on that topic makes failed messages inspectable instead of silently discarding them.

## Provisioning Order

`pubsub.sh` will perform the following idempotent sequence:

1. Validate required local configuration and enabled Secret Manager versions.
2. Enable the Pub/Sub API.
3. Create the primary and dead-letter topics if absent.
4. Resolve the Google-managed Pub/Sub service agent.
5. Grant it `roles/iam.serviceAccountTokenCreator` for authenticated push delivery.
6. Grant it `roles/pubsub.publisher` on the dead-letter topic.
7. Grant the API runtime service account `roles/pubsub.publisher`.
8. Deploy both private worker services from the same image as the API using request-based CPU and zero minimum instances.
9. Grant the push identity `roles/run.invoker` on both workers.
10. Create missing filtered subscriptions or update existing subscriptions with their push endpoint, authentication, acknowledgement deadline, retry policy, and dead-letter policy.
11. Grant the Pub/Sub service agent `roles/pubsub.subscriber` on every source subscription so dead-letter forwarding works.
12. Create a non-expiring dead-letter inspection subscription if absent.
13. Run read-only infrastructure verification.
14. Only after all prior steps succeed, update the API to `PUBSUB_ENABLED=true` while retaining request-based CPU and zero minimum instances.

Keeping the API switch last prevents it from publishing into a partially configured worker system.

## Subscription Policy

Every source subscription will use:

- its existing immutable `attributes.task_type` filter;
- message ordering enabled at creation;
- a 600-second acknowledgement deadline;
- authenticated push with the worker runtime service account;
- a minimum retry delay of 10 seconds;
- a maximum retry delay of 600 seconds;
- `location-tasks-dead-letter` as its dead-letter topic;
- ten maximum delivery attempts;
- no automatic subscription expiration.

The dead-letter inspection subscription will not push messages. It will retain them for manual inspection and acknowledgement.

## Cloud Run Billing and Scaling

The deployment scripts will specify `--cpu-throttling` explicitly for the API and both worker services. CPU is therefore allocated only while a request is executing. Each service will specify `--min-instances 0`, allowing it to scale to zero. Pub/Sub provides durability and wakes workers by sending requests; no background work is expected to continue after a worker response.

## Authentication and IAM

The API runtime service account publishes task messages. The same user-managed service account is the authenticated Pub/Sub push identity and invokes the private workers. The Google-managed Pub/Sub service agent signs push tokens and performs dead-letter forwarding.

Required bindings:

- API runtime service account: `roles/pubsub.publisher` on the project or primary topic.
- Push identity: `roles/run.invoker` on each worker service.
- Pub/Sub service agent: `roles/iam.serviceAccountTokenCreator` on the project.
- Pub/Sub service agent: `roles/pubsub.publisher` on the dead-letter topic.
- Pub/Sub service agent: `roles/pubsub.subscriber` on each source subscription.

## Verification

Local validation will include:

- `bash -n deploy.sh pubsub.sh verify_pubsub.sh`
- ShellCheck when installed
- focused tests for the task worker and Pub/Sub payload handling
- inspection of the exact generated `gcloud` commands without running production deployment

Post-deployment verification will include:

- API and both worker revisions report ready;
- all three revisions explicitly report request-based CPU and zero minimum instances;
- the API reports `PUBSUB_ENABLED=true`;
- all six subscriptions have the correct filter, endpoint, retry policy, dead-letter policy, ordering, and authentication identity;
- dead-letter IAM bindings are present;
- authenticated `/health` requests to both workers return 200;
- one controlled `/locations/add` request produces a publish log, worker consume log, and acknowledgement log;
- the corresponding source subscription has no growing undelivered-message backlog;
- the dead-letter subscription remains empty after the smoke test.

## Rollback

Rollback keeps Cloud Run request-based and restores the current code path:

1. Set `PUBSUB_ENABLED=false` on the API.
2. Clear push configuration on the six source subscriptions so queued messages are retained as pull messages rather than repeatedly delivered.
3. Confirm the API starts its in-process fallback workers.
4. Inspect pending and dead-letter messages before either resuming push delivery or explicitly acknowledging them.

Worker services and topics do not need to be deleted during rollback.

## Deployment Documentation

A repository deployment guide will provide the exact commands for prerequisites, deployment, verification, smoke testing, monitoring, dead-letter inspection, and rollback. Running `DEPLOY_PUBSUB=true ./deploy.sh` will remain the single supported production entry point.

