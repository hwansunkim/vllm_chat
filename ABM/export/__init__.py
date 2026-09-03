"""시뮬레이션 결과를 사람이 읽는 문서로 내보내는 계층.

현재는 스크린플레이 마크다운 하나뿐이다. 브라우저 쪽 쌍둥이 구현이
``frontend/js/sim/export/markdown.js`` 에 있고 출력이 글자 단위로 같아야 한다
(``tests/fixtures/*.md`` 골든 테스트가 이 파이썬 쪽을 고정한다).
"""

from .markdown import render_markdown

__all__ = ["render_markdown"]
