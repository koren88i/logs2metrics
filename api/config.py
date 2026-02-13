"""Service configuration from environment variables."""

import os

ES_URL: str = os.getenv("ES_URL", "http://elasticsearch:9200")
KIBANA_URL: str = os.getenv("KIBANA_URL", "http://kibana:5601")
HEALTH_CHECK_INTERVAL: int = int(os.getenv("HEALTH_CHECK_INTERVAL", "60"))
