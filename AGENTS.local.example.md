# 이 컴퓨터의 로컬 설정 (예시)

이 파일을 `AGENTS.local.md`로 복사해 이 컴퓨터의 값을 채운다. `AGENTS.local.md`는 git에 올리지 않는다(`.gitignore`). `AGENTS.md` 6절의 `<…>` 자리에 아래 값을 쓴다.

## 실행 파일

| 이름 | 값 |
| --- | --- |
| `<vllm python>` | 예: `C:\Users\<사용자>\miniconda3\envs\vllm\python.exe` / `~/miniconda3/envs/vllm/bin/python` |
| `<fastapi python>` | 예: `C:\Users\<사용자>\miniconda3\envs\fastapi\python.exe` |
| `<tectonic>` | 예: `C:\Users\<사용자>\miniconda3\envs\tex\Library\bin\tectonic.exe` (LaTeX를 쓰지 않으면 비워 둔다) |
| `<codex>` | 예: `C:\Users\<사용자>\AppData\Roaming\npm\codex.cmd` / `codex` (PATH에 있으면) |

## LLM 서버

| 항목 | 값 |
| --- | --- |
| 시뮬레이션 기본 LLM 서버 | 예: `VLLM_BASE_URL=http://<호스트>:8000` (환경 변수로 지정) |
| 기본 모델 | 예: `VLLM_MODEL=<모델 이름>` |

## 이 컴퓨터의 특이 사항

- 예: Git Bash에서는 npm 전역 경로가 PATH에 없어 `<codex>`를 전체 경로로 실행한다.
- 예: Windows PowerShell에서 한글 출력이 깨지면 `PYTHONUTF8=1`을 설정한다.
