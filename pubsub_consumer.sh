PROJECT="project-add4b0f5-0080-47ef-80f"
gcloud pubsub subscriptions describe location-tasks-pipeline \
  --project "$PROJECT" \
  --format="yaml(name,topic,filter,pushConfig,enableMessageOrdering)"