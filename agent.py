import sys
import logging
import requests
from ABM.config import BASE_URL
from ABM.scenarios.convenience_store import run

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

if __name__ == "__main__":
    try:
        requests.get(f"{BASE_URL}/health", timeout=5).raise_for_status()
    except Exception as e:
        print(f"❌ 서버 연결 실패: {e}")
        sys.exit(1)

    run()
