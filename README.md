# vLLM Web Chat

로컬 vLLM 서버와 연동하는 멀티 대화 웹 채팅 클라이언트.  
RAG 기반 메모리 시스템으로 컨텍스트 윈도우를 효율적으로 관리한다.

## 실행

```bash
pip install -r requirements.txt
python run.py
# → http://localhost:8888
```

또는 uvicorn을 직접 실행할 수 있다.

```bash
uvicorn backend.main:app --host 0.0.0.0 --port 8888 --reload
```

vLLM 서버 주소와 모델은 웹 UI의 서버 관리 화면 또는 `/api/servers` API로 등록한다.
초기 서버 목록은 `servers.json` 파일이 있을 때 첫 실행 시 DB에 시드된다.

## 문서

- [아키텍처](docs/architecture.md) — 전체 시스템 구조
- [메모리 시스템](docs/memory-system.md) — RAG 메모리 동작 원리
- [API 레퍼런스](docs/api.md) — REST API 명세
- [Vector DB 전환 검토](docs/vectordb.md) — SQLite → Vector DB 마이그레이션 고려사항
