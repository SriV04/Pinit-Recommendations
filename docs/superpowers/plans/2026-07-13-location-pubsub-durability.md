# Location Pub/Sub Durability Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Deploy durable, retried location-processing tasks through authenticated Pub/Sub push subscriptions while every Cloud Run service remains request-based and scales to zero.

**Architecture:** Harden the existing `location-tasks` topic, filtered subscriptions, and two private Cloud Run workers. Provision retry and dead-letter policies before switching the API from its in-process fallback, then verify configuration and one no-op production task end to end.

**Tech Stack:** Bash, Google Cloud CLI, Cloud Run, Pub/Sub, Secret Manager, Python `unittest`/`pytest`.

---

### Task 1: Pin the deployment contract with failing tests

**Files:**
- Create: `tests/test_pubsub_deployment_scripts.py`
- Inspect: `deploy.sh`
- Inspect: `pubsub.sh`

- [ ] **Step 1: Write tests for request-based CPU and durable Pub/Sub policy**

Create tests that read the two scripts and assert:

```python
def test_all_cloud_run_deployments_use_request_based_cpu_and_scale_to_zero():
    assert api_deploy_has("--cpu-throttling")
    assert fast_worker_deploy_has("--cpu-throttling")
    assert menu_worker_deploy_has("--cpu-throttling")
    assert all_deploys_have("--min-instances 0")
    assert "--no-cpu-throttling" not in combined_scripts


def test_pubsub_provisions_retry_and_dead_letter_policy_before_enabling_api():
    assert "DEAD_LETTER_TOPIC" in pubsub_script
    assert "--min-retry-delay" in pubsub_script
    assert "--max-retry-delay" in pubsub_script
    assert "--max-delivery-attempts" in pubsub_script
    assert "roles/iam.serviceAccountTokenCreator" in pubsub_script
    assert "roles/pubsub.publisher" in pubsub_script
    assert "roles/pubsub.subscriber" in pubsub_script
    assert pubsub_script.index("subscriptions update") < pubsub_script.index("PUBSUB_ENABLED=true")
```

- [ ] **Step 2: Run the tests and confirm they fail for the missing contract**

Run:

```bash
python -m pytest -q tests/test_pubsub_deployment_scripts.py
```

Expected: failures showing missing `--cpu-throttling`, dead-letter policy, retry policy, and IAM configuration.

### Task 2: Harden the production deployment scripts

**Files:**
- Modify: `deploy.sh`
- Modify: `pubsub.sh`
- Test: `tests/test_pubsub_deployment_scripts.py`

- [ ] **Step 1: Make API request-based billing explicit**

Add the following to the API `gcloud run deploy` command in `deploy.sh`:

```bash
--cpu-throttling \
--min-instances 0 \
```

Keep `DEPLOY_PUBSUB=true ./deploy.sh` as the supported entry point.

- [ ] **Step 2: Add idempotent topic, IAM, worker, and subscription provisioning**

Update `pubsub.sh` so it:

```bash
DEAD_LETTER_TOPIC="${PUBSUB_DEAD_LETTER_TOPIC:-${TOPIC}-dead-letter}"
DEAD_LETTER_SUBSCRIPTION="${PUBSUB_DEAD_LETTER_SUBSCRIPTION:-${DEAD_LETTER_TOPIC}-inspect}"
MIN_RETRY_DELAY="${PUBSUB_MIN_RETRY_DELAY:-10s}"
MAX_RETRY_DELAY="${PUBSUB_MAX_RETRY_DELAY:-600s}"
MAX_DELIVERY_ATTEMPTS="${PUBSUB_MAX_DELIVERY_ATTEMPTS:-10}"
```

Then provision the two topics, required service-agent IAM, both workers with `--cpu-throttling --min-instances 0`, six authenticated push subscriptions, the pull dead-letter inspection subscription, and API activation last.

For existing subscriptions, validate immutable filters and ordering before updating mutable push/retry/dead-letter settings. Fail rather than silently delete or recreate a mismatched production subscription.

- [ ] **Step 3: Re-run the deployment contract tests**

Run:

```bash
python -m pytest -q tests/test_pubsub_deployment_scripts.py
```

Expected: all tests pass.

### Task 3: Add read-only verification and operator documentation

**Files:**
- Create: `verify_pubsub.sh`
- Create: `docs/deploying-location-pubsub.md`
- Modify: `tests/test_pubsub_deployment_scripts.py`

- [ ] **Step 1: Extend the failing tests for the verification script**

Assert that `verify_pubsub.sh` checks:

```python
assert "cpu-throttling" in verification_script
assert "minScale" in verification_script
assert "PUBSUB_ENABLED" in verification_script
assert "deadLetterPolicy" in verification_script
assert "retryPolicy" in verification_script
assert "pushConfig" in verification_script
```

Run the focused test and confirm it fails because `verify_pubsub.sh` does not exist.

- [ ] **Step 2: Implement `verify_pubsub.sh`**

The script must use only read-only `gcloud ... describe/get-iam-policy` commands. It must fail unless:

- API and workers are ready;
- CPU throttling is absent/true, never false;
- minimum scale is absent/zero;
- API has `PUBSUB_ENABLED=true`;
- every subscription has the expected filter, push endpoint, authentication service account, retry policy, dead-letter policy, ordering, and 600-second acknowledgement deadline;
- required project/topic/subscription IAM bindings exist.

- [ ] **Step 3: Write the deployment guide**

Document these exact phases:

```bash
gcloud auth list
gcloud config set project pinit-494520
DEPLOY_PUBSUB=true ./deploy.sh
./verify_pubsub.sh
```

Also document authenticated worker health checks, a controlled `/locations/add` smoke request, log queries, dead-letter inspection, and rollback commands that restore `PUBSUB_ENABLED=false` without enabling permanent CPU.

- [ ] **Step 4: Run focused tests and shell validation**

Run:

```bash
python -m pytest -q tests/test_pubsub_deployment_scripts.py tests/test_location_tasks.py tests/test_api_endpoints.py -k 'pubsub or location_task or locations_add'
bash -n deploy.sh pubsub.sh verify_pubsub.sh
```

Expected: tests pass and Bash reports no syntax errors.

### Task 4: Review and commit the implementation

**Files:**
- Modify: `deploy.sh`
- Modify: `pubsub.sh`
- Create: `verify_pubsub.sh`
- Create: `docs/deploying-location-pubsub.md`
- Create: `tests/test_pubsub_deployment_scripts.py`

- [ ] **Step 1: Review the diff for billing and scope safety**

Run:

```bash
git diff --check
git diff -- deploy.sh pubsub.sh verify_pubsub.sh docs/deploying-location-pubsub.md tests/test_pubsub_deployment_scripts.py
rg -n -- '--no-cpu-throttling|--min-instances [1-9]' deploy.sh pubsub.sh verify_pubsub.sh docs/deploying-location-pubsub.md
```

Expected: clean diff; the final search has no matches except documentation explicitly warning against prohibited settings.

- [ ] **Step 2: Run the full relevant verification suite**

Run:

```bash
python -m pytest -q tests/test_pubsub_deployment_scripts.py tests/test_location_tasks.py tests/test_api_endpoints.py
bash -n deploy.sh pubsub.sh verify_pubsub.sh
```

Expected: all tests pass and all scripts parse.

- [ ] **Step 3: Commit only this task's files**

```bash
git add deploy.sh pubsub.sh verify_pubsub.sh docs/deploying-location-pubsub.md tests/test_pubsub_deployment_scripts.py docs/superpowers/plans/2026-07-13-location-pubsub-durability.md
git commit -m "fix: make location processing durable with Pub/Sub"
```

### Task 5: Deploy and verify production

**Files:**
- Read: `docs/deploying-location-pubsub.md`

- [ ] **Step 1: Record the pre-deployment state**

Capture current API revision, worker revisions, CPU billing annotations, minimum scale, API `PUBSUB_ENABLED`, and existing subscriptions.

- [ ] **Step 2: Deploy through the supported entry point**

Run:

```bash
DEPLOY_PUBSUB=true ./deploy.sh
```

Do not run any command containing `--no-cpu-throttling` or a nonzero minimum instance count.

- [ ] **Step 3: Run infrastructure verification immediately**

Run:

```bash
./verify_pubsub.sh
```

If any CPU or minimum-scale assertion fails, immediately set `PUBSUB_ENABLED=false` and stop.

- [ ] **Step 4: Run one controlled end-to-end no-op task**

Select an existing fully processed location with `updated_vibe=true`, call `/locations/add` with `source=in-app`, and confirm logs contain the same request ID for publish, consume, and acknowledgement without a database enrichment mutation.

- [ ] **Step 5: Confirm queue and dead-letter health**

Verify source subscription backlog is not growing and the dead-letter inspection subscription received no smoke-test message. Report exact revisions, task request ID, and verification results.

