#!/bin/bash

# Exit if any of the intermediate steps fail
set -e

# TODO: Test to see if this checks for the socket
# Check if docker is installed
if ! command -v docker &> /dev/null; then
    echo "Error: docker is not installed" >/dev/stderr
    exit 1
fi

ECR_REGISTRY="$(aws sts get-caller-identity --query Account --output text).dkr.ecr.$REGION.amazonaws.com"

echo $HASH

# LAMBDA_PATH exists and is a directory
if [ ! -d "$LAMBDA_PATH" ]; then
    echo "Error: $LAMBDA_PATH is not a directory" >/dev/stderr
    exit 1
fi

# main.py exists
if [ ! -f "$LAMBDA_PATH/main.py" ]; then
    echo "Error: main.py not found in $LAMBDA_PATH" >/dev/stderr
    exit 1
fi

## pyproject.toml exists in the lambda_path directory
#if [ ! -f "$LAMBDA_PATH/pyproject.toml" ]; then
#    echo "Error: pyproject.toml not found in $LAMBDA_PATH" >/dev/stderr
#    exit 2
#fi
#
## TODO: uv lock file only needed if pyproject has dependencies (maybe pyproject not needed if no dependencies?)
## uv.lock exists in the lambda_path directory
#if [ ! -f "$LAMBDA_PATH/uv.lock" ]; then
#    echo "Error: uv.lock not found in $LAMBDA_PATH" >/dev/stderr
#    exit 3
#fi

# Dockerfile as a string
DOCKERFILE_TEMPLATE=$(cat <<'EOF'
FROM public.ecr.aws/lambda/python:3.14-arm64 AS builder

# Install uv
#RUN pip install --no-cache-dir "uv==0.12.4"
#ENV UV_PYTHON_DOWNLOADS=0
#
#COPY pyproject.toml uv.lock ${LAMBDA_TASK_ROOT}/
#RUN uv export --frozen --no-dev --no-emit-project -o /tmp/requirements.txt && \
#    uv pip install --target "${LAMBDA_TASK_ROOT}" -r /tmp/requirements.txt
#
FROM public.ecr.aws/lambda/python:3.14-arm64
COPY --from=builder ${LAMBDA_TASK_ROOT} ${LAMBDA_TASK_ROOT}
COPY . ${LAMBDA_TASK_ROOT}

CMD ["main.handler"]
EOF
)


# TODO: Deal with limits/pagination on list call
# List images in the ECR repository
IMAGES=$(aws ecr list-images --repository-name "$ECR_REPOSITORY" --query 'imageIds[*].imageTag' --output text)
echo $IMAGES

# Check if an image with the same hash already exists in the ECR repository
IMAGE_EXISTS=false
echo "$IMAGES" | grep -qw "$HASH" && IMAGE_EXISTS=true

# If image with the same hash exists, pull it from ECR
# TODO: Pulling to do security scanning locally. Fail on critical/high CVEs
# TODO: If it exists on ECR and Local, decide on what to do if the image sha hashes are different
if [ "$IMAGE_EXISTS" = true ]; then
    echo "Image already exists in ECR. Skipping build..."
    aws ecr get-login-password | docker login --username AWS --password-stdin "$(aws sts get-caller-identity --query Account --output text).dkr.ecr.$REGION.amazonaws.com" > /dev/null
    docker pull "$ECR_REGISTRY/$ECR_REPOSITORY:$HASH" > /dev/null
else
    echo "Building image and pushing to ECR"
    aws ecr get-login-password | docker login --username AWS --password-stdin "$(aws sts get-caller-identity --query Account --output text).dkr.ecr.$REGION.amazonaws.com" > /dev/null
    printf '%s\n' "$DOCKERFILE_TEMPLATE" | docker buildx build \
        --platform linux/arm64 \
        --provenance=false \
        --load \
        -f - \
        -t "$ECR_REGISTRY/$ECR_REPOSITORY:$HASH" \
        "$LAMBDA_PATH"
fi

# Scan the image with grype
echo "Scanning image with grype"
docker run --rm -v /var/run/docker.sock:/var/run/docker.sock -v `pwd`:/tmp/grype-report anchore/grype:latest "$ECR_REGISTRY/$ECR_REPOSITORY:$HASH" --fail-on $VULNERABILITY_SCANNER_THRESHOLD --quiet -o sarif --file /tmp/grype-report/grype.sarif


# Push the image to ECR after passing the vulnerability scan with the provided threshold
docker push "$ECR_REGISTRY/$ECR_REPOSITORY:$HASH" > /dev/null
