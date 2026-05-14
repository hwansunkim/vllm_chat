# vLLM Web Chat

로컬 vLLM 서버와 연동하는 멀티 대화 웹 채팅 클라이언트.  
RAG 기반 메모리 시스템으로 컨텍스트 윈도우를 효율적으로 관리한다.

## 실행

```bash
pip install -r requirements.txt
python server.py
# → http://localhost:8888
```

vLLM 서버 주소와 모델은 `chat.py` 상단의 상수에서 변경한다.

```python
BASE_URL = "http://172.17.3.135:8000"
MODEL    = "google/gemma-4-31B-it"
```

## CLI 모드

웹 서버 없이 터미널에서 직접 사용할 수 있다.

```bash
python chat.py
```

## 문서

- [아키텍처](docs/architecture.md) — 전체 시스템 구조
- [메모리 시스템](docs/memory-system.md) — RAG 메모리 동작 원리
- [API 레퍼런스](docs/api.md) — REST API 명세
- [Vector DB 전환 검토](docs/vectordb.md) — SQLite → Vector DB 마이그레이션 고려사항
