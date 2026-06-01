import os

BASE_URL    = os.environ.get("VLLM_BASE_URL",     "http://172.17.3.135:8000")
MODEL       = os.environ.get("VLLM_MODEL",         "Qwen/Qwen3.6-35B-A3B")
API_KEY     = os.environ.get("VLLM_API_KEY",       "local")
LOG_DIR     = os.environ.get("ABM_LOG_DIR",        "logs_graph")
TOKEN_LIMIT = int(os.environ.get("ABM_TOKEN_LIMIT", "8192"))
API_TIMEOUT = int(os.environ.get("ABM_API_TIMEOUT", "120"))
