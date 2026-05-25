import os

BASE_URL     = os.environ.get("VLLM_BASE_URL",      "http://172.17.3.135:8000")
MODEL        = os.environ.get("VLLM_MODEL",          "Qwen/Qwen3.6-35B-A3B")
LOG_DIR      = os.environ.get("ABM_LOG_DIR",         "logs_graph")
MEMORY_LIMIT = int(os.environ.get("ABM_MEMORY_LIMIT", "20"))
API_TIMEOUT  = int(os.environ.get("ABM_API_TIMEOUT",  "120"))
