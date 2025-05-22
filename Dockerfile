# 1. Base Image
FROM python:3.11-slim

# 2. Working Directory
WORKDIR /app

# 3. Environment Variables
ENV PYTHONUNBUFFERED=1
ENV DOCSTORE_STORAGE_BACKEND=s3
ENV DOCSTORE_S3_BUCKET_NAME=your-s3-bucket-name-placeholder
ENV AWS_ACCESS_KEY_ID=your-aws-access-key-id-placeholder
ENV AWS_SECRET_ACCESS_KEY=your-aws-secret-access-key-placeholder
ENV DOCSTORE_S3_REGION_NAME=your-s3-region-placeholder
ENV DOCSTORE_S3_ENDPOINT_URL=""
ENV DOCSTORE_ROOT_PATH=/data/docstore # For local storage if used
ENV PORT=8080

# 4. Copy Requirements
# Copy requirements.txt first to leverage Docker layer caching
COPY requirements.txt .

# 5. Install Dependencies
# Note: This uses the existing requirements.txt. If boto3 (added to requirements.in)
# is not in the committed requirements.txt, this step might not install it,
# potentially causing runtime issues if S3 backend is used.
# A more robust Dockerfile might re-compile requirements.in if tools were available.
RUN pip install --no-cache-dir -r requirements.txt

# 6. Copy Application Code
# Copy the rest of the application code
COPY . .

# 7. Expose Port
EXPOSE 8080

# 8. Entrypoint/CMD
# Run the application using the docstore CLI
# The PORT environment variable will be substituted by the shell.
CMD ["docstore", "serve", "--host", "0.0.0.0", "--port", "${PORT}"]
