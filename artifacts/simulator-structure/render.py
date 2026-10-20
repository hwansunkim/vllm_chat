"""Render source-based simulator block diagrams as SVG and PNG (no external service)."""
from pathlib import Path
from html import escape
import math
from PIL import Image, ImageDraw, ImageFont

OUT = Path(__file__).resolve().parent
FONT = Path('C:/Windows/Fonts/malgun.ttf')
BOLD = Path('C:/Windows/Fonts/malgunbd.ttf')
INK = '#192B45'
MUTED = '#586A80'
LINE = '#8393A6'
BLUE = '#2864B5'
TEAL = '#147D76'
PURPLE = '#7954AC'
ORANGE = '#B46A22'
BG = '#F6F8FC'


class Diagram:
    def __init__(self, width, height, title, desc):
        self.width, self.height = width, height
        self.image = Image.new('RGB', (width, height), BG)
        self.draw = ImageDraw.Draw(self.image)
        self.svg = [f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}" role="img" aria-labelledby="title desc">',
                    f'<title id="title">{escape(title)}</title><desc id="desc">{escape(desc)}</desc>',
                    '<style>text{font-family:"Malgun Gothic","Noto Sans KR",sans-serif}</style>',
                    f'<rect width="{width}" height="{height}" fill="{BG}"/>']

    def rect(self, x, y, w, h, fill='white', stroke='#D8E0EA', radius=16, sw=2):
        assert 0 <= x and 0 <= y and x + w <= self.width and y + h <= self.height
        self.svg.append(f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="{radius}" fill="{fill}" stroke="{stroke}" stroke-width="{sw}"/>')
        self.draw.rounded_rectangle((x, y, x+w, y+h), radius, fill, stroke, sw)

    def text(self, x, y, text, size=22, color=INK, bold=False, max_width=None):
        font_path = BOLD if bold else FONT
        font = ImageFont.truetype(str(font_path), size)
        if max_width:
            while font.getlength(text) > max_width and size > 13:
                size -= 1
                font = ImageFont.truetype(str(font_path), size)
            assert font.getlength(text) <= max_width, text
        self.svg.append(f'<text x="{x}" y="{y}" font-size="{size}" font-weight="{700 if bold else 400}" fill="{color}">{escape(text)}</text>')
        self.draw.text((x, y), text, font=font, fill=color, anchor='ls')

    def arrow(self, points, color=LINE, dashed=False, both=False, label=None, label_at=None):
        pts = ' '.join(f'{x},{y}' for x, y in points)
        dash = ' stroke-dasharray="7 6"' if dashed else ''
        self.svg.append(f'<polyline points="{pts}" fill="none" stroke="{color}" stroke-width="3" stroke-linejoin="round"{dash}/>')
        for a, b in zip(points, points[1:]):
            if dashed:
                dx, dy = b[0]-a[0], b[1]-a[1]
                length = math.hypot(dx, dy)
                for t in range(0, int(length), 13):
                    end = min(t+7, length)
                    self.draw.line((a[0]+dx*t/length, a[1]+dy*t/length, a[0]+dx*end/length, a[1]+dy*end/length), fill=color, width=3)
            else:
                self.draw.line((a, b), fill=color, width=3)
        def head(a, b):
            angle = math.atan2(b[1]-a[1], b[0]-a[0])
            tri = [b, (b[0]-12*math.cos(angle-.42), b[1]-12*math.sin(angle-.42)), (b[0]-12*math.cos(angle+.42), b[1]-12*math.sin(angle+.42))]
            self.svg.append(f'<polygon points="{" ".join(f"{x:.1f},{y:.1f}" for x,y in tri)}" fill="{color}"/>')
            self.draw.polygon(tri, fill=color)
        head(points[-2], points[-1])
        if both:
            head(points[1], points[0])
        if label:
            x, y = label_at
            font = ImageFont.truetype(str(FONT), 18)
            self.rect(x-7, y-23, math.ceil(font.getlength(label))+14, 31, BG, BG, 6, 1)
            self.text(x, y, label, 18, color)

    def box(self, x, y, w, h, title, lines=(), code=None, color=BLUE, fill='white', title_size=24):
        self.rect(x, y, w, h, fill, '#D4DFEA')
        self.rect(x+17, y+21, 5, 27, color, color, 2, 1)
        self.text(x+34, y+38, title, title_size, color, True, w-53)
        for i, line in enumerate(lines):
            self.text(x+23, y+72+i*25, line, 21, INK, max_width=w-46)
        if code:
            self.text(x+23, y+h-16, code, 16, MUTED, max_width=w-46)

    def save(self, name):
        (OUT / f'{name}.svg').write_text('\n'.join(self.svg + ['</svg>'])+'\n', encoding='utf-8')
        self.image.save(OUT / f'{name}.png', optimize=True)


def architecture():
    d = Diagram(1800, 1440, '시뮬레이터 전체 구조', '웹 UI와 CLI가 공유하는 ABM 엔진, LLM 호출 계층, 저장소와 결과 출력의 구조. 현재 저장소 코드를 기준으로 작성.')
    d.text(64, 64, '시뮬레이터 전체 구조', 41, bold=True)
    d.text(66, 102, '현재 코드 기준 · 웹 / CLI 공용 ABM 엔진 · 주요 모듈과 데이터 흐름', 22, MUTED)
    d.text(1530, 64, '2026.10.06', 20, MUTED)

    d.rect(64, 140, 1180, 160, '#EDF4FE', '#CADBF1')
    d.text(87, 176, '01  사용자 인터페이스', 22, BLUE, True)
    d.box(88, 192, 354, 86, '시나리오 설정', ['인물 · 관계 · 지도 · 시간 · 규칙'], color=BLUE, title_size=22)
    d.box(477, 192, 354, 86, '실행 제어', ['시작 · 중지 · 계속 · 복원'], color=BLUE, title_size=22)
    d.box(866, 192, 354, 86, '관전·분석', ['피드 · D3 지도 / 그래프 · 기억 조회'], color=BLUE, title_size=22)
    d.box(1290, 140, 446, 160, '헤드리스 CLI', ['시나리오 JSON · 반복 / 배치 실행', 'GUI와 동일한 엔진 호출'], 'ABM/cli.py', TEAL)

    d.arrow([(270, 300), (270, 360)], BLUE, label='REST 설정·실행 요청', label_at=(295, 337))
    d.arrow([(1090, 360), (1090, 300)], BLUE, label='SSE 실시간 이벤트', label_at=(855, 337))
    d.box(64, 360, 1180, 145, '02  FastAPI 시뮬레이션 API', ['설정 검증 · 시나리오 / 서버 관리 · 실행 상태 관리 · 시작 / 계속 / 재개', '이벤트 큐 → SSE 스트림 · 로그 / 컨텍스트 조회 · 실행 이력 / 인터뷰'], 'backend/api/simulation/  ·  backend/main.py', BLUE, '#F7FAFF')
    d.arrow([(650, 505), (650, 562)], BLUE, label='설정 + LLM 콜러블 + DB + 이벤트 큐', label_at=(200, 543))
    d.arrow([(1110, 562), (1110, 505)], BLUE, label='실행 이벤트', label_at=(1128, 542))
    d.arrow([(1513, 300), (1513, 327), (1266, 327), (1266, 648), (1220, 648)], TEAL)
    d.text(1320, 365, '웹: 백그라운드 스레드 실행', 21, MUTED)
    d.text(1320, 399, 'CLI: 배치별 실행·로그 분리', 21, MUTED)
    d.text(1320, 452, '새 실행 조립은 run_config() 공유', 20, TEAL)
    d.text(1320, 483, '계속 / 재개는 run()으로 진입', 20, TEAL)

    d.rect(64, 562, 1180, 558, '#EAF6F3', '#BADBD4')
    d.text(88, 600, '03  ABM 시뮬레이션 엔진', 26, TEAL, True)
    d.text(853, 600, 'Simulation = 11개 기능 믹스인', 19, TEAL)
    d.box(88, 620, 1132, 80, '공용 초기화: run_config() → Agent × N + Simulation', [], 'ABM/simulation/headless.py  ·  core.py', TEAL, '#FFFFFF', 24)
    d.arrow([(268, 700), (268, 739)], TEAL)

    d.box(88, 739, 354, 166, 'Wave 실행 제어', ['참가자 선정 · 병렬 턴 실행', '발화 전달 · 다음 Wave 구성'], 'simulation/runner.py', TEAL)
    d.box(477, 739, 354, 166, 'Agent × N', ['페르소나 · 입력 / 기억 · 프롬프트', 'LLM 판단 → JSON 응답 해석'], 'agent.py · step.py · turn.py', TEAL)
    d.box(866, 739, 354, 166, '세계 규칙과 상태', ['위치 / zone · 이동 / 여정 · 만남', '인지 · 활동 상태 · 감염 SEPIR(S)'], 'location / targets / status / infection', TEAL)
    d.arrow([(442, 816), (477, 816)], TEAL)
    d.arrow([(831, 816), (866, 816)], TEAL)
    d.arrow([(1040, 905), (1040, 930), (265, 930), (265, 905)], TEAL)
    d.rect(494, 915, 320, 30, '#EAF6F3', '#EAF6F3', 5, 1)
    d.text(514, 937, '세계 상태 · 다음 Wave 입력', 19, TEAL)

    d.box(88, 965, 354, 130, '기억 관리', ['최근 메시지 ↔ 구조화 장기 기억'], 'memory_compressor.py · db/', PURPLE)
    d.box(477, 965, 354, 130, '시나리오·디렉터', ['예약 사건 · 침묵 / 반복 감지·개입'], 'events.py · system.py', ORANGE)
    d.box(866, 965, 354, 130, '시뮬레이션 시간', ['고정 / 가변 경과 · 점프 제한'], 'runner.py · time_classifier.py', TEAL)

    d.rect(1290, 562, 446, 367, '#F3EEFA', '#D7C8E9')
    d.text(1314, 603, '04  LLM 호출 계층', 25, PURPLE, True)
    d.box(1314, 626, 398, 112, '동기 → 비동기 브릿지', ['워커 요청을 이벤트 루프로 전달'], 'backend/llm/bridge.py', PURPLE)
    d.arrow([(1513, 738), (1513, 777)], PURPLE, both=True)
    d.box(1314, 777, 398, 128, '서버 레지스트리·프로바이더', ['vLLM · OpenAI · Anthropic'], 'backend/llm/registry.py · providers/', PURPLE)
    d.arrow([(1244, 707), (1290, 707)], PURPLE, both=True)
    d.text(1304, 976, 'LLM을 사용하는 기능', 22, PURPLE, True)
    d.text(1304, 1014, '에이전트 판단 · 기억 압축 / 정리', 21, MUTED)
    d.text(1304, 1045, '디렉터 개입 · 가변 시간 추론', 21, MUTED)
    d.text(1304, 1080, '이동·감염 등 상태 반영은 엔진이 수행', 20, TEAL)

    d.arrow([(928, 1120), (928, 1195)], TEAL, both=True, label='기록 / 기억 조회 / 상태 복원', label_at=(625, 1162))
    d.arrow([(1200, 1120), (1200, 1155), (1513, 1155), (1513, 1195)], ORANGE, label='JSON 로그', label_at=(1304, 1146))
    d.arrow([(64, 430), (30, 430), (30, 1262), (64, 1262)], BLUE, both=True)
    d.box(64, 1195, 540, 173, 'memory.db', ['시나리오 정의 · LLM 서버 설정', '채팅 에이전트 · 채팅 대화 / 메모리'], 'backend/db/database.py', BLUE)
    d.box(640, 1195, 604, 173, 'logs_graph/simulation.db', ['실행 이력 · 턴 / 이벤트 · 재개용 스냅샷', '에피소드 · 사실 · 관계 · 자아 상태 기억'], 'ABM/db/  (SimDB)', TEAL)
    d.box(1290, 1195, 446, 173, '기록·결과 활용', ['JSON 로그 · Markdown / CSV', '리플레이 · 실행 재개 · 인터뷰'], 'ABM/export/ · frontend/js/sim/', ORANGE)
    d.arrow([(1244, 1280), (1290, 1280)], ORANGE)
    d.text(67, 1412, '화살표: 주요 호출·데이터 경로  |  양방향: 요청 / 응답 또는 저장 / 조회  |  기능은 시나리오 설정에 따라 활성화', 18, MUTED)
    d.save('architecture')


def wave_full():
    d = Diagram(1800, 1700, 'Wave 내부 처리와 에이전트 판단·기억 흐름', 'runner.py의 정상 진행 순서를 요약하고 에이전트 한 명의 프롬프트, LLM, 응답 파싱과 기억 압축 경로를 펼친 도식.')
    d.text(64, 64, 'Wave 내부 처리와 에이전트 판단·기억 흐름', 39, bold=True)
    d.text(66, 108, 'Wave = 현재 차례를 받은 에이전트들을 병렬로 처리하는 한 라운드', 23, MUTED)
    d.text(64, 166, 'A  엔진의 정상 진행 경로', 26, TEAL, True)
    d.text(946, 166, 'B  에이전트 한 명의 턴', 26, BLUE, True)

    steps = [
        (205, 134, '1  Wave 준비', ['예약 이벤트 · 상태 만료 → 이번 참가자 결정', '디렉터 개입(설정 시) · 이동 중 입력 보류'], 'runner.py · events.py · status.py · system.py'),
        (384, 111, '2  에이전트 병렬 실행', ['ThreadPoolExecutor → _step_agent() × 참가자 수'], 'step.py · 결과 수집 후 키순 정규화'),
        (540, 134, '3  대화·관찰 전달 계산', ['이동 전 위치 기준으로 발화 대상과 도달성 판정', 'spatial 모드: 주변 발화 엿듣기 · 행동 관찰 추가'], 'targets.py · runner.py::_route_spatial'),
        (719, 134, '4  세계 상태 갱신', ['외모 · 활동 상태 → 이동 / 만남 / 여정 / 동행', '장소·접촉·실제 경과 시간으로 감염 모델 계산'], 'location.py · meeting.py · journey.py · status.py · infection.py'),
        (898, 134, '5  다음 Wave 구성', ['발화 / 씬 메시지 전달 또는 보류', '소외 에이전트 재투입 · 침묵 / 진행 불가 처리'], 'runner.py · status.py'),
        (1077, 134, '6  시간 진행', ['고정 간격 / 가변 시간: LLM 분류·분 추론·idle', '가변 점프는 예정 이벤트·상태 해제 등으로 제한'], 'runner.py · time_classifier.py'),
        (1256, 111, '7  계속 실행 여부 판정', ['Wave 상한 · 목표 기간 · 중지 · 진행 상태 확인'], '반복 또는 simulation_end'),
    ]
    for y, h, title, lines, code in steps:
        d.box(80, y, 740, h, title, lines, code, TEAL)
    for previous, nxt in zip(steps, steps[1:]):
        d.arrow([(450, previous[0]+previous[1]), (450, nxt[0])], TEAL)
    d.arrow([(270, 1367), (270, 1410), (34, 1410), (34, 264), (80, 264)], TEAL, label='다음 Wave', label_at=(92, 1401))
    d.arrow([(450, 1367), (450, 1460)], ORANGE, label='종료', label_at=(472, 1423))
    d.box(80, 1460, 740, 120, '종료·재개 준비', ['실행 결과 / 스냅샷 저장 · SSE 종료 · 내보내기'], 'API finalize_run() / CLI 완료 처리', ORANGE)

    d.rect(934, 205, 802, 895, '#EDF4FE', '#CADBF1')
    d.text(962, 248, '참가자마다 독립적인 Agent·기억·LLM 설정', 23, BLUE, True)
    d.box(960, 275, 750, 112, '입력 주입·상황 조립', ['받은 말 / 관찰 → 최근 기억 · 현재 위치 / 사람 / 증상 계산'], '_inject_incoming() · _assemble_agent_prompt()', BLUE)
    d.arrow([(1335, 387), (1335, 427)], BLUE)
    d.box(960, 427, 750, 204, '컨텍스트 관리·프롬프트 구성', ['페르소나 + 엔진 세계 규칙 / JSON 출력 계약', '배경 + 구조화 장기 기억 + 최근 메시지', '매 턴 재계산한 현재 시각 · 상황 · 몸 상태'], '_maybe_compress() → trim → Agent.build_messages()', BLUE)
    d.arrow([(1335, 631), (1335, 671)], BLUE)
    d.box(960, 671, 750, 106, 'LLM 호출', ['에이전트별 서버 / temperature 우선, 없으면 공통값'], '동기 콜러블 → 브릿지 → 프로바이더', PURPLE)
    d.arrow([(1335, 777), (1335, 817)], BLUE)
    d.box(960, 817, 750, 113, '응답 해석·턴 반영', ['대사 · 대상 · 행동 · move_to · enter_state 등'], 'parser.py · turn.py::_apply_turn_result()', BLUE)
    d.arrow([(1335, 930), (1335, 970)], BLUE)
    d.box(960, 970, 750, 106, '기억·로그 기록 + 결과 반환', ['자신의 응답 저장 · turn_complete · DB 턴 로그'], 'Agent.memory · shared_log · simulation_log', BLUE)
    d.arrow([(820, 438), (879, 438), (879, 330), (934, 330)], BLUE, label='각 참가자', label_at=(824, 309))
    d.arrow([(934, 1020), (901, 1020), (901, 512), (760, 512), (760, 540)], BLUE)
    d.text(962, 1127, '실패 시 입력 롤백 / 오류 이벤트; 정상 결과만 이후 처리', 18, MUTED)

    d.rect(934, 1160, 802, 420, '#F3EEFA', '#D7C8E9')
    d.text(961, 1205, 'C  구조화 장기 기억으로 이어지는 순환', 25, PURPLE, True)
    d.text(962, 1241, '컨텍스트가 임계값에 도달하면 압축 (DB 사용 시)', 21, MUTED)
    d.box(960, 1270, 346, 171, '기억 압축·정리', ['원문 → 기존 사실 후보 검색', 'LLM이 새 사실 / 갱신 판단', '주기적으로 사실 확신도 정리'], 'memory_compressor.py', PURPLE, title_size=22)
    d.box(1350, 1270, 360, 171, 'SimDB 구조화 기억', ['에피소드 · 사실', '관계 · 자아 상태', '원문 / 근거 연결 보존'], 'ABM/db/', PURPLE, title_size=22)
    d.arrow([(1306, 1355), (1350, 1355)], PURPLE)
    d.arrow([(1710, 531), (1773, 531), (1773, 1220), (1736, 1220)], PURPLE, dashed=True)
    d.arrow([(1530, 1441), (1530, 1500), (1338, 1500)], PURPLE)
    d.text(962, 1507, '현재 시점 기준 기억 블록 생성', 23, PURPLE, True)
    d.text(962, 1543, '다음 판단에 주입 · 압축 직후에도 최신 기억 사용', 21, MUTED)
    d.arrow([(934, 1495), (862, 1495), (862, 589), (960, 589)], PURPLE, dashed=True)

    d.text(65, 1637, '실선: 정상 처리 / 결과 전달   ·   점선: 조건부 압축과 기억 조회 경로', 20, MUTED)
    d.text(65, 1674, '정상 경로를 요약한 도식이며 중지·오류·활성 에이전트 없음 등은 중간에도 종료할 수 있습니다. 이벤트는 각 단계에서 수시로 발생합니다.', 18, MUTED)
    d.save('wave-full')


def wave():
    from render_infection import render
    render()


if __name__ == '__main__':
    architecture()
    wave_full()
    wave()
    print('Rendered architecture, wave-full, and focused wave/infection-scenario diagrams')
