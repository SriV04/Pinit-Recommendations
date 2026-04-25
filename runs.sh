# 0) Set your project + topic name
PROJECT="project-add4b0f5-0080-47ef-80f"                 # change me
TOPIC="location-tasks"                # must match PUBSUB_TOPIC_LOCATION_TASKS

# 1) Make sure the API is enabled
gcloud services enable pubsub.googleapis.com --project "$PROJECT"

# 2) Create the topic
gcloud pubsub topics create "$TOPIC" --project "$PROJECT"

# 3) Create one subscription per task_type (attribute filter) + enable ordering
for TASK in pipeline details_enrich emoji photos menu_vibe vibe_reprocess; do
  gcloud pubsub subscriptions create "${TOPIC}-${TASK}" \
    --project "$PROJECT" \
    --topic "$TOPIC" \
    --message-filter="attributes.task_type=\"${TASK}\"" \
    --enable-message-ordering
done
