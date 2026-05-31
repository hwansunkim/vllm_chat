import os
import json
import networkx as nx
import matplotlib
matplotlib.use('Agg')  # GUI 없는 환경에서도 동작 (plt.show() 대신 savefig 사용)
import matplotlib.pyplot as plt
import matplotlib.font_manager as fm


# --- 1. 한글 폰트 설정 ---

def setup_korean_font() -> str | None:
    """
    시스템에서 한글 폰트 파일을 찾아 matplotlib에 등록하고 기본 폰트로 설정.
    - 파일명 기반 탐색으로 폰트 이름 불일치 문제를 피함
    - Variable 폰트는 matplotlib FreeType 렌더러와 호환 문제가 있어 건너뜀
    - font.sans-serif 리스트 선두에 삽입해 우선 적용
    """
    priority = ['nanum', 'notosanscjk', 'notosanskr', 'malgun', 'gulim', 'dotum', 'd2coding', 'unbatang']

    all_paths = (
        fm.findSystemFonts(fontpaths=None, fontext='ttf')
        + fm.findSystemFonts(fontpaths=None, fontext='otf')
    )

    for keyword in priority:
        for path in sorted(all_paths):
            basename = os.path.basename(path).lower()
            if 'variable' in basename:   # variable 폰트 제외
                continue
            if keyword in basename:
                try:
                    fm.fontManager.addfont(path)
                    font_name = fm.FontProperties(fname=path).get_name()
                    current = plt.rcParams.get('font.sans-serif', [])
                    plt.rcParams['font.sans-serif'] = [font_name] + [f for f in current if f != font_name]
                    plt.rcParams['font.family'] = 'sans-serif'
                    plt.rcParams['axes.unicode_minus'] = False
                    print(f"✅ 한글 폰트 적용: {font_name}  ({os.path.basename(path)})")
                    return font_name
                except Exception:
                    continue

    print("⚠️ 한글 폰트를 찾지 못했습니다. 기본 폰트를 사용합니다.")
    return None


setup_korean_font()


# --- 2. 데이터 로드 ---
try:
    with open("logs_graph/edges.json", "r", encoding='utf-8') as f:
        edges = json.load(f)
except FileNotFoundError:
    print("❌ edges.json 파일을 찾을 수 없습니다. 먼저 시뮬레이션을 실행해주세요.")
    exit()

if not edges:
    print("❌ edges.json이 비어 있습니다.")
    exit()


# --- 3. 그래프 생성 ---
G = nx.DiGraph()

for edge in edges:
    G.add_edge(edge['source'], edge['target'], label=edge['emotion'])

name_mapping = {
    "boss":     "김민태 (사장님)",
    "lee":      "이상민 (알바1)",
    "park":     "박슬기 (알바2)",
    "customer": "정용진 (손님)",
    "system":   "system",
}


# --- 4. 그래프 시각화 ---
plt.figure(figsize=(15, 10))

pos = nx.spring_layout(G, seed=42, k=0.5)

nx.draw_networkx_nodes(G, pos, node_color='lightblue', node_size=3000, alpha=0.9)

node_labels = {node: name_mapping.get(node, node) for node in G.nodes()}
nx.draw_networkx_labels(G, pos, labels=node_labels, font_size=12, font_weight='bold')

nx.draw_networkx_edges(G, pos, arrows=True, arrowstyle='-|>', arrowsize=20, edge_color='gray')

edge_labels = nx.get_edge_attributes(G, 'label')
nx.draw_networkx_edge_labels(G, pos, edge_labels=edge_labels, font_size=10, font_color='red')

plt.title("편의점 에이전트 상호작용 그래프", fontsize=16, pad=20)
plt.axis('off')
plt.tight_layout()

output_path = "logs_graph/graph.png"
plt.savefig(output_path, dpi=150, bbox_inches='tight')
plt.close()

print(f"✅ 그래프가 저장되었습니다: {os.path.abspath(output_path)}")
