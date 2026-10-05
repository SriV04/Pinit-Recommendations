from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
DEPLOY_SCRIPT = (REPO_ROOT / "deploy.sh").read_text(encoding="utf-8")
PUBSUB_SCRIPT = (REPO_ROOT / "pubsub.sh").read_text(encoding="utf-8")


def _command_block(script: str, marker: str) -> str:
    start = script.index(marker)
    end = script.find("\n\n", start)
    return script[start:] if end == -1 else script[start:end]


def test_all_cloud_run_deployments_use_request_based_cpu_and_scale_to_zero() -> None:
    api = _command_block(DEPLOY_SCRIPT, "gcloud run deploy $SERVICE_NAME")
    fast_worker = _command_block(
        PUBSUB_SCRIPT,
        'gcloud run deploy "${WORKER_FAST_SERVICE}"',
    )
    menu_worker = _command_block(
        PUBSUB_SCRIPT,
        'gcloud run deploy "${WORKER_MENU_SERVICE}"',
    )

    for block in (api, fast_worker, menu_worker):
        assert "--cpu-throttling" in block
        assert "--min-instances 0" in block
        assert "--no-cpu-throttling" not in block


def test_pubsub_provisions_retry_and_dead_letter_policy_before_enabling_api() -> None:
    required_fragments = (
        "DEAD_LETTER_TOPIC",
        "--dead-letter-topic",
        "--min-retry-delay",
        "--max-retry-delay",
        "--max-delivery-attempts",
        "roles/iam.serviceAccountTokenCreator",
        "roles/pubsub.publisher",
        "roles/pubsub.subscriber",
    )
    for fragment in required_fragments:
        assert fragment in PUBSUB_SCRIPT

    subscription_config = PUBSUB_SCRIPT.index("gcloud pubsub subscriptions update")
    api_activation = PUBSUB_SCRIPT.index('gcloud run services update "${API_SERVICE}"')
    assert subscription_config < api_activation

    assert 'if [ "${DEPLOY_PUBSUB:-false}" = "true" ]; then' in DEPLOY_SCRIPT
    assert 'PUBSUB_ENABLED="false"' in DEPLOY_SCRIPT


def test_process_location_subscription_routes_to_menu_worker_during_cutover() -> None:
    assert "process_location" in PUBSUB_SCRIPT
    assert (
        'configure_task_subscription "process_location" "${MENU_WORKER_URL}"'
        in PUBSUB_SCRIPT
    )

    verification_script = (REPO_ROOT / "verify_pubsub.sh").read_text(encoding="utf-8")
    assert "process_location" in verification_script
    assert (
        'verify_subscription "process_location" "${MENU_WORKER_URL}"'
        in verification_script
    )


def test_pubsub_worker_deployments_are_private_and_request_driven() -> None:
    for marker in (
        'gcloud run deploy "${WORKER_FAST_SERVICE}"',
        'gcloud run deploy "${WORKER_MENU_SERVICE}"',
    ):
        block = _command_block(PUBSUB_SCRIPT, marker)
        assert "--no-allow-unauthenticated" in block
        assert "--cpu-throttling" in block
        assert "--min-instances 0" in block


def test_verification_script_checks_runtime_and_subscription_contract() -> None:
    verification_path = REPO_ROOT / "verify_pubsub.sh"
    assert verification_path.exists()
    verification_script = verification_path.read_text(encoding="utf-8")

    for fragment in (
        "cpu-throttling",
        "minScale",
        "PUBSUB_ENABLED",
        "deadLetterPolicy",
        "retryPolicy",
        "pushConfig",
        "roles/iam.serviceAccountTokenCreator",
        "roles/pubsub.publisher",
        "roles/pubsub.subscriber",
    ):
        assert fragment in verification_script


def test_deploy_script_refuses_to_ship_the_live_service_to_full_traffic_by_default() -> None:
    import subprocess

    # The guard runs before any docker/gcloud call, so this is safe to execute.
    result = subprocess.run(
        ["bash", str(REPO_ROOT / "deploy.sh")],
        capture_output=True,
        text=True,
        env={"PATH": "/usr/bin:/bin"},
        cwd=REPO_ROOT,
    )

    assert result.returncode != 0
    assert "Refusing to deploy the live service" in result.stdout
    assert "NO_TRAFFIC=true" in result.stdout


def test_deploy_script_supports_a_zero_traffic_canary_revision() -> None:
    assert '--no-traffic --tag "${TRAFFIC_TAG}"' in DEPLOY_SCRIPT
    assert "IMAGE_TAG" in DEPLOY_SCRIPT
    assert "--min-instances 0" in DEPLOY_SCRIPT


def test_workers_receive_r2_credentials_from_secret_manager_only() -> None:
    assert "R2_ACCESS_KEY_ID=${r2_secret_name}" not in PUBSUB_SCRIPT  # bound per pair below
    assert 'FAST_SECRETS+=",${r2_env_name}=${r2_secret_name}:latest"' in PUBSUB_SCRIPT
    assert 'MENU_SECRETS+=",${r2_env_name}=${r2_secret_name}:latest"' in PUBSUB_SCRIPT
    # Never copied into plain env vars by the .env passthrough.
    assert "R2_ACCESS_KEY_ID|R2_SECRET_ACCESS_KEY)" in PUBSUB_SCRIPT
