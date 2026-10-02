FROM python:3.13-slim

WORKDIR /app

# Install dependencies (cached layer). Minimal package stub lets setuptools build.
COPY pyproject.toml README.md ./
RUN mkdir -p upwork_scraper && touch upwork_scraper/__init__.py \
    && pip install --no-cache-dir .

# Copy real source (invalidates only on code changes)
COPY upwork_scraper/ upwork_scraper/

# Continuous feed on stdout. Override with any CLI flags you like, e.g.
#   docker run --rm upwork-scraper --pages 5 --query react --format csv
ENTRYPOINT ["python", "-m", "upwork_scraper"]
CMD ["--watch", "--format", "jsonl"]
