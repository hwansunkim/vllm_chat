"""시뮬레이션 결과를 사람이 읽는 문서로 내보내는 계층.

두 가지 포맷이 있고 둘 다 브라우저 쪽 쌍둥이 구현과 출력이 같아야 한다.
  - 스크린플레이 마크다운 — ``frontend/js/sim/export/markdown.js``
    (``tests/fixtures/*.md`` 골든 테스트가 이 파이썬 쪽을 고정한다)
  - 위치 이력 CSV        — ``frontend/js/sim/export/csv.js``

둘 다 DB가 아니라 ``shared_log`` / ``events`` 를 직접 받으므로 ``--no-db`` 실행에서도
쓸 수 있다.
"""

from .csv import render_location_csv
from .markdown import render_markdown

__all__ = ["render_markdown", "render_location_csv"]
